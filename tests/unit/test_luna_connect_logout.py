"""Connect / logout example tests for Luna (task 10.3).

Concrete scenario tests (not Hypothesis) for the two ends of a Luna
session's life: the interactive sign-in (:class:`LunaAuth`, task 10.2 over
:class:`LunaBrowserAuth`, task 10.1) and the logout
(:meth:`LunaStore.logout`, which clears the Amazon cookies through
``cookie_writer.py`` via :class:`LunaSessionMonitor`).

Luna has no OAuth: "connected" means the interactive Amazon sign-in left
non-expired Amazon session cookies in the Luna profile. These tests mock the
browser / CDP / session seams (no real browser, no Amazon) following the
existing Luna launch-flow harness style (``test_luna_launch_flow.py`` /
``test_luna_launch_integration.py``): every collaborator :class:`LunaAuth`
and :class:`LunaStore` inject is a fake, and the clock/sleep on the poll loop
are driven synthetically so a 300 s budget resolves in-process.

Covered scenarios (task 10.3):

* successful connect — sign-in observed + cookies persisted → ``connected``;
* not-connected cases — profile-permission (1.9), profile-not-persisted
  (8.4), early close (1.5), no cookies (1.6), timeout (1.8),
  no supported browser — each with the right reason and partial state
  discarded;
* logout clears cookies and reports not_connected (8.7), with the no-cookies
  auto-logout still reporting not_connected (8.9);
* logout clear-failure retains the session and reports it (8.8).

_Requirements: 1.5, 1.8, 1.9, 8.4, 8.7, 8.8, 8.9_
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from unifideck.core.types import AuthResult, Events, Result
from unifideck.stores.luna import luna_store as store_module
from unifideck.stores.luna.luna_auth import (
    ERR_NO_BROWSER,
    ERR_PROFILE_NOT_PERSISTED,
    ERR_PROFILE_PERMISSION,
    ERR_SIGN_IN_CLOSED,
    ERR_SIGN_IN_NO_COOKIES,
    ERR_SIGN_IN_TIMEOUT,
    SIGN_IN_TIMEOUT_S,
    LunaAuth,
)
from unifideck.stores.luna.luna_browser_auth import ObservedAuthState
from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_profile import LunaProfileError
from unifideck.stores.luna.luna_store import LUNA_STORE, LunaStatus, LunaStore

# ── Shared fakes ──────────────────────────────────────────────────────

#: A fixed, non-``/tmp`` profile path used across the fakes. The dir is never
#: created or written — every filesystem seam is mocked — so this is only an
#: opaque identifier the ``profile_permission`` / ``profile_not_persisted``
#: errors carry through to ``AuthResult.url``.
_PROFILE_DIR = "/home/deck/.local/share/unifideck/luna"


def _config() -> LunaConfig:
    """A Luna config with the fields the sign-in URL + profile need."""
    return LunaConfig(
        luna_host="https://luna.amazon.de",
        signin_path="/signin",
        profile_dir=_PROFILE_DIR,
    )


class _FakeClock:
    """A monotonic clock the poll loop drives; ``sleep`` advances it.

    Lets :meth:`LunaAuth.run_sign_in` reach its 300 s timeout branch without
    real waiting: each injected ``sleep(dt)`` just moves ``now`` forward.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, dt: float) -> None:
        self.now += dt


class _FakeProfile:
    """Stand-in for :class:`LunaProfile`.

    ``ensure_error`` / ``verify_error`` inject the two abort points
    (profile-permission at connect, profile-not-persisted after spawn).
    """

    def __init__(
        self,
        *,
        ensure_error: LunaProfileError | None = None,
        verify_error: LunaProfileError | None = None,
    ) -> None:
        self._ensure_error = ensure_error
        self._verify_error = verify_error

    def ensure_profile_dir(self) -> str:
        if self._ensure_error is not None:
            raise self._ensure_error
        return _PROFILE_DIR

    def verify_persisted(self) -> None:
        if self._verify_error is not None:
            raise self._verify_error


class _FakeBrowser:
    """Stand-in for :class:`LunaBrowser`: launch / kill / cdp_port + process."""

    def __init__(self, *, launched: bool = True, alive: bool = True) -> None:
        self._launched = launched
        self.killed = 0
        proc = SimpleNamespace(poll=lambda: None if alive else 0)
        self.edge = SimpleNamespace(process=proc)

    def launch(self, url: str) -> bool:
        return self._launched

    def kill(self) -> None:
        self.killed += 1

    def cdp_port(self) -> int:
        return 9223


class _FakeCDP:
    """A CDP client whose port probe answers immediately."""

    def probe_cdp(self) -> bool:
        return True


class _FakeBrowserAuth:
    """Stand-in for :class:`LunaBrowserAuth` returning scripted observations.

    ``states`` is consumed one per poll; the last is repeated so the loop can
    be driven to timeout. ``signed_in`` ends the loop as connected.
    """

    def __init__(self, states: list[ObservedAuthState]) -> None:
        self._states = states
        self._i = 0

    def observe_signed_in(self, cdp: Any) -> ObservedAuthState:
        state = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return state


def _observed(
    *, signed_in: bool = False, left_signin: bool = False, cookies: bool = False,
) -> ObservedAuthState:
    return ObservedAuthState(
        signed_in=signed_in, cdp_left_signin=left_signin, cookies_present=cookies,
    )


def _auth(
    *,
    session: Any,
    browser: _FakeBrowser | None = None,
    profile: Any | None = None,
    browser_auth: Any | None = None,
    clock: _FakeClock | None = None,
) -> LunaAuth:
    """Build a :class:`LunaAuth` with every seam faked and the clock injected."""
    clock = clock or _FakeClock()
    browser = browser if browser is not None else _FakeBrowser()
    fake_profile = profile or _FakeProfile()
    return LunaAuth(
        None,
        browser,
        session,
        _config(),
        browser_auth=browser_auth or _FakeBrowserAuth([_observed()]),
        profile=fake_profile,
        spoofer_factory=lambda _c: SimpleNamespace(attach=AsyncMock()),
        cdp_factory=lambda _port: _FakeCDP(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


# ── start_auth: successful connect ─────────────────────────────────────


async def test_connect_signin_observed_and_cookies_persisted() -> None:
    """Sign-in observed + cookies persisted → connected (Req 1.4)."""
    session = MagicMock()
    session.is_usable.return_value = True
    auth = _auth(
        session=session,
        browser_auth=_FakeBrowserAuth([_observed(signed_in=True, left_signin=True, cookies=True)]),
    )

    result = await auth.start_auth()

    assert isinstance(result, AuthResult)
    assert result.success is True
    assert result.store == LUNA_STORE
    assert result.tokens_cached is True
    assert result.error is None
    # A successful connect never discards the freshly-persisted session.
    session.clear_cookies.assert_not_called()


# ── start_auth: not-connected cases ────────────────────────────────────


async def test_connect_profile_not_writable_aborts_before_browser() -> None:
    """A non-writable profile dir aborts before any browser opens (Req 1.9)."""
    session = MagicMock()
    browser = _FakeBrowser()
    fake_profile = _FakeProfile(
        ensure_error=LunaProfileError(
            "profile_permission", _PROFILE_DIR, "not writable",
        ),
    )
    auth = _auth(session=session, browser=browser, profile=fake_profile)

    result = await auth.start_auth()

    assert result.success is False
    assert result.error == ERR_PROFILE_PERMISSION
    assert result.url == _PROFILE_DIR
    # No browser opened, nothing to discard.
    assert browser.killed == 0


async def test_connect_profile_not_persisted_after_spawn() -> None:
    """An absent profile dir after spawn → profile_not_persisted (Req 8.4)."""
    session = MagicMock()
    fake_profile = _FakeProfile(
        verify_error=LunaProfileError(
            "profile_not_persisted", _PROFILE_DIR,
            "profile dir absent after browser start",
        ),
    )
    browser = _FakeBrowser()
    auth = _auth(session=session, browser=browser, profile=fake_profile)

    result = await auth.start_auth()

    assert result.success is False
    assert result.error == ERR_PROFILE_NOT_PERSISTED
    assert result.url == _PROFILE_DIR
    # The kiosk was torn down after the aborted attempt.
    assert browser.killed >= 1


async def test_connect_no_supported_browser() -> None:
    """A browser that fails to launch → no_supported_browser."""
    session = MagicMock()
    browser = _FakeBrowser(launched=False)
    auth = _auth(session=session, browser=browser)

    result = await auth.start_auth()

    assert result.success is False
    assert result.error == ERR_NO_BROWSER


async def test_connect_early_close_discards_partial_state() -> None:
    """Window closed before cookies land → closed + partial state discarded (Req 1.5)."""
    session = MagicMock()
    session.is_usable.return_value = False
    browser = _FakeBrowser(alive=False)  # window already gone on first poll
    auth = _auth(session=session, browser=browser)

    result = await auth.start_auth()

    assert result.success is False
    assert result.error == ERR_SIGN_IN_CLOSED
    # Partial session state discarded (cookies cleared) on the not-connected path.
    session.clear_cookies.assert_called_once()
    assert browser.killed >= 1


async def test_connect_timeout_never_left_signin_discards_state() -> None:
    """300 s elapses with the UI never leaving sign-in → timeout (Req 1.8)."""
    session = MagicMock()
    session.is_usable.return_value = False
    clock = _FakeClock()
    # Never signed in, never left the sign-in page: times out as 'timeout'.
    auth = _auth(
        session=session,
        clock=clock,
        browser_auth=_FakeBrowserAuth([_observed(signed_in=False, left_signin=False)]),
    )

    result = await auth.start_auth()

    assert result.success is False
    assert result.error == ERR_SIGN_IN_TIMEOUT
    # The budget was actually consumed by the injected clock.
    assert clock.now >= 1000.0 + SIGN_IN_TIMEOUT_S
    session.clear_cookies.assert_called_once()


async def test_connect_timeout_after_signin_but_no_cookies() -> None:
    """UI reached Luna but no cookies persisted before timeout → no_cookies (Req 1.6)."""
    session = MagicMock()
    session.is_usable.return_value = False
    auth = _auth(
        session=session,
        # Left the sign-in page (reached Luna) but cookies never persisted.
        browser_auth=_FakeBrowserAuth([_observed(signed_in=False, left_signin=True)]),
    )

    result = await auth.start_auth()

    assert result.success is False
    assert result.error == ERR_SIGN_IN_NO_COOKIES
    session.clear_cookies.assert_called_once()


# ── run_sign_in: connect-on-close (user finished then closed window) ───


async def test_run_sign_in_window_closed_with_cookies_is_connected() -> None:
    """A closed window with cookies present still counts as connected (Req 1.5)."""
    session = MagicMock()
    session.is_usable.return_value = True
    browser = _FakeBrowser(alive=False)
    auth = _auth(session=session, browser=browser)

    outcome = await auth.run_sign_in(_FakeCDP())

    assert outcome.status == "connected"
    assert outcome.error is None


# ── LunaStore.logout ───────────────────────────────────────────────────


def _store(session: Any, *, bus: Any = None) -> LunaStore:
    """A :class:`LunaStore` built without running ``__init__``.

    Mirrors the Microsoft store-auth test's ``__new__`` construction: only
    the attributes ``logout`` touches are populated, so no real config,
    browser, or CDP is involved.
    """
    store = LunaStore.__new__(LunaStore)
    store._session = session
    store._browser = _FakeBrowser()
    store._bus = bus or SimpleNamespace(emit=AsyncMock())
    return store


async def test_logout_clears_cookies_and_reports_not_connected() -> None:
    """Logout clears the Amazon cookies and reports not_connected (Req 8.7)."""
    session = MagicMock()
    session.clear_cookies.return_value = True
    bus = SimpleNamespace(emit=AsyncMock())
    store = _store(session, bus=bus)

    result = await store.logout()

    assert isinstance(result, Result)
    assert result.success is True
    assert result.store == LUNA_STORE
    assert result.metadata[store_module.STATUS_KEY] == LunaStatus.NOT_CONNECTED
    session.clear_cookies.assert_called_once()
    # The browser (which owns the cookie DB) is killed before the clear.
    assert store._browser.killed >= 1
    bus.emit.assert_awaited_once_with(Events.STORE_LOGOUT, store=LUNA_STORE)


async def test_logout_with_no_cookies_still_reports_not_connected() -> None:
    """A no-op clear (no cookies to remove) still reports not_connected (Req 8.9).

    ``clear_cookies`` returns True on the nothing-to-delete case, so an
    already-logged-out profile resolves to not_connected rather than an error.
    """
    session = MagicMock()
    session.clear_cookies.return_value = True  # no-op clear succeeds
    bus = SimpleNamespace(emit=AsyncMock())
    store = _store(session, bus=bus)

    result = await store.logout()

    assert result.success is True
    assert result.metadata[store_module.STATUS_KEY] == LunaStatus.NOT_CONNECTED
    bus.emit.assert_awaited_once_with(Events.STORE_LOGOUT, store=LUNA_STORE)


async def test_logout_clear_failure_retains_session_and_reports_it() -> None:
    """A failed clear retains the session and reports it (Req 8.8).

    When ``clear_cookies`` returns False the previously persisted session is
    kept: logout reports failure with status still CONNECTED and does NOT
    emit the logout event (nothing was torn down session-wise).
    """
    session = MagicMock()
    session.clear_cookies.return_value = False
    bus = SimpleNamespace(emit=AsyncMock())
    store = _store(session, bus=bus)

    result = await store.logout()

    assert result.success is False
    assert result.error == "logout_failed"
    assert result.metadata[store_module.STATUS_KEY] == LunaStatus.CONNECTED
    bus.emit.assert_not_awaited()


async def test_logout_clear_raising_is_treated_as_failure() -> None:
    """A clear that raises is caught and treated as a retained session (Req 8.8)."""
    session = MagicMock()
    session.clear_cookies.side_effect = OSError("db locked")
    bus = SimpleNamespace(emit=AsyncMock())
    store = _store(session, bus=bus)

    result = await store.logout()

    assert result.success is False
    assert result.error == "logout_failed"
    assert result.metadata[store_module.STATUS_KEY] == LunaStatus.CONNECTED
    bus.emit.assert_not_awaited()
