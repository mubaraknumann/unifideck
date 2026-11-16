"""Property test for Luna platform-spoof navigation invariance (task 5.6).

Feature: amazon-luna-streaming, Property 2: Navigation invariance

**Property 2:** For any sequence of navigations within a Luna browser
session (detail page -> player -> any further Luna page), the platform value
reported by ``navigator.userAgentData.getHighEntropyValues()`` equals the
configured Windows value at every step, including on the first page before
any navigation. (This is the invariant whose violation at page-level-only
scope was the observed real failure mode F3.)

**Validates: Requirements 14.4, 14.10, 14.11**

The design note allows Property 2 to use a local harness page driven by a
real headless browser; here it is kept as a **mock-CDP unit test** (no real
browser, no Amazon) so it runs in CI without a GPU/display. The mock
``_NavigatingBrowser`` stands in for the browser-level ``BrowserCdpSession``:
every navigation / reload / new-document load spawns a *fresh* CDP target
that enters ``waitForDebuggerOnStart``, and the mock reports the spoofed
Windows platform for a page **only if** the per-new-document override script
(``Page.addScriptToEvaluateOnNewDocument``) was installed on that target
*before* it was released via ``Runtime.runIfWaitingForDebugger``. A
page-level-only override that failed to re-apply on navigation would read
back ``"Linux"`` here (reproducing F3). Driving the real
``LunaPlatformSpoofer.run_attach`` (browser-level ``Target.setAutoAttach``)
against this mock and then ``run_verify`` after each navigation asserts the
override persists across the whole session.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st

from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_platform_spoofer import (
    LunaPlatformSpoofer,
    build_spoof_identity,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_WINDOWS = "Windows"
_LINUX = "Linux"

_UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
)

_CONFIG = LunaConfig(
    luna_host="luna.amazon.de",
    allowed_stream_hosts=["luna.amazon.de"],
    signin_path="/signin",
    player_path="/player",
    profile_dir="/home/deck/.local/share/unifideck/luna-profile",
    browser_game_cdp_flavour="browser_game",
    spoof_ua_template=_UA_TEMPLATE,
    spoof_uach_platform=_WINDOWS,
    spoof_navigator_platform="Win32",
    spoof_webgl_renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX, Direct3D11)",
)

#: The identity the spoofer would derive at runtime from the real browser.
#: Fixed here (version tracking is Property 3's concern); Property 2 only
#: cares that the override is *applied and persists* across navigations.
_IDENTITY = build_spoof_identity(_CONFIG, "Chrome/150.0.7871.186")


class _Target:
    """One CDP target (page or service worker) in ``waitForDebuggerOnStart``.

    A target models the F3 failure mode: it reports the spoofed Windows
    platform back from ``getHighEntropyValues()`` **only** once the
    per-new-document override script has been installed (``scripted``) *and*
    the UA/UA-CH override applied (``ua_override``) — and only if both landed
    *before* the target was resumed (``resumed_after_override``). A real
    page-level-only override that did not re-apply on navigation would leave
    ``scripted`` False on the fresh target, so the probe would read Linux.
    """

    def __init__(self, session_id: str, target_type: str) -> None:
        self.session_id = session_id
        self.target_type = target_type
        self.ua_override = False
        self.scripted = False
        self.resumed = False
        self.resumed_after_override = False

    @property
    def spoofed(self) -> bool:
        """Whether this target reports the Windows platform (override took)."""
        return self.ua_override and self.scripted and self.resumed_after_override

    def platform(self) -> str:
        """The platform ``getHighEntropyValues(['platform'])`` reports here."""
        return _WINDOWS if self.spoofed else _LINUX


class _NavigatingBrowser:
    """Mock browser-level ``BrowserCdpSession`` with scripted navigations.

    ``pending`` is the queue of targets the auto-attach loop will see (the
    detail page, then one fresh target per navigation / reload / new-document
    load, interleaved with service workers). ``active`` is the page the
    browser is currently showing — the one ``run_verify`` probes. Commands
    carrying a ``session_id`` mutate that target; the browser-level
    ``Target.setAutoAttach`` carries none.
    """

    def __init__(self, targets: list[_Target]) -> None:
        self._by_session = {t.session_id: t for t in targets}
        self._pending = list(targets)
        self.auto_attach_enabled = False
        # The active page is the most recent *page* target (last navigation).
        self.active: _Target | None = None
        self._page_order = [t for t in targets if t.target_type == "page"]

    # ── BrowserCdpSession protocol ──────────────────────────────────
    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        if method == "Target.setAutoAttach":
            # Must be the browser-level call (no session id) with flatten +
            # waitForDebuggerOnStart so every current/future target is caught.
            assert session_id is None
            assert params.get("flatten") is True
            assert params.get("waitForDebuggerOnStart") is True
            self.auto_attach_enabled = True
            return {}
        if method == "Target.getTargets":
            # verify() asks for targets to find the active page to probe.
            infos = [
                {"targetId": t.session_id, "type": t.target_type}
                for t in self._by_session.values()
                if t is self.active
            ]
            return {"targetInfos": infos}
        if method == "Target.attachToTarget":
            target_id = params.get("targetId")
            return {"sessionId": target_id}

        # Per-target commands require a session id (flatten routing).
        assert session_id is not None, f"{method} needs a session id"
        target = self._by_session[session_id]
        return self._dispatch_target_command(method, target)

    def _dispatch_target_command(
        self, method: str, target: _Target,
    ) -> dict[str, Any]:
        if method == "Emulation.setUserAgentOverride":
            target.ua_override = True
            return {}
        # The CDP ``Page`` domain only exists on real ``page`` targets. A
        # service_worker (which Luna spins up) rejects ``Page.*`` with
        # ``-32601`` ("wasn't found") exactly as a real browser does; the
        # spoofer must tolerate it and still resume the target.
        if method in ("Page.enable", "Page.addScriptToEvaluateOnNewDocument"):
            if target.target_type != "page":
                raise RuntimeError(
                    f"{method} failed: {{'code': -32601, 'message': "
                    f'"\'{method}\' wasn\'t found"}}}}',
                )
            if method == "Page.addScriptToEvaluateOnNewDocument":
                target.scripted = True
            return {}
        if method == "Runtime.runIfWaitingForDebugger":
            target.resumed = True
            # Record whether the full override landed before the resume.
            target.resumed_after_override = target.ua_override and target.scripted
            # Resuming a page target makes it the one the browser shows.
            if target.target_type == "page":
                self.active = target
            return {}
        if method == "Runtime.evaluate":
            # The verify() platform probe runs here against the active page.
            return {
                "result": {"type": "string", "value": target.platform()},
            }
        raise AssertionError(f"unexpected per-target command {method}")

    async def attached_targets(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(session_id, target_info)`` for each navigation target."""
        for target in self._pending:
            yield target.session_id, {"type": target.target_type}


# ── Generators ──────────────────────────────────────────────────────
#
# A navigation sequence is: the first page (detail), then 0..N further
# navigations/reloads/new-document loads, each producing a FRESH page target,
# optionally interleaved with service-worker targets (which Luna spins up and
# which must also be covered — Req 14.10). The order and count are free.

_TARGET_KIND = st.sampled_from(["page", "service_worker"])


@st.composite
def _navigation_sequences(draw: Any) -> list[_Target]:
    """Build an ordered target list: always >=1 page, maybe service workers."""
    kinds = draw(st.lists(_TARGET_KIND, min_size=0, max_size=8))
    # Property 2 requires at least the first page "before any navigation".
    kinds = ["page", *kinds]
    targets: list[_Target] = []
    for index, kind in enumerate(kinds):
        targets.append(_Target(session_id=f"sess-{index}", target_type=kind))
    return targets


def _page_targets(targets: list[_Target]) -> list[_Target]:
    return [t for t in targets if t.target_type == "page"]


@settings(max_examples=200)
@given(targets=_navigation_sequences())
async def test_platform_is_windows_at_every_navigation_step(
    targets: list[_Target],
) -> None:
    """verify() reports Windows at every page, including the first (Req 14.4/14.11).

    The real ``run_attach`` installs the browser-level auto-attach and, as
    each navigation's fresh target appears, applies the full override before
    the target is released. After each page becomes active, ``run_verify``
    probes it and must read the configured Windows platform — never Linux —
    which is exactly the navigation-invariance the spoofer guarantees via the
    ``addScriptToEvaluateOnNewDocument`` re-application on every new document.
    """
    pages = _page_targets(targets)

    # Drive each navigation step: process targets up to and including the
    # i-th page, then probe. This simulates "navigate, then observe" for the
    # detail page, the player page, and any further page in order.
    for step, _page in enumerate(pages):
        # Build a browser whose pending queue is the prefix of targets up to
        # the step-th page (inclusive) so verify() probes that page as active.
        prefix = _prefix_through_page(targets, step)
        browser = _NavigatingBrowser(prefix)
        spoofer = LunaPlatformSpoofer(_CONFIG)

        await spoofer.run_attach(browser, _IDENTITY)
        reported = await spoofer.run_verify(browser)

        assert browser.auto_attach_enabled is True
        assert reported == _WINDOWS, (
            f"navigation step {step}: expected Windows, got {reported!r} "
            "(override failed to persist across navigation — F3)"
        )


@settings(max_examples=200)
@given(targets=_navigation_sequences())
async def test_full_override_applied_before_resume_on_every_target(
    targets: list[_Target],
) -> None:
    """Every target (page or SW) gets the full override before it is resumed.

    Navigation invariance rests on this: a target released from
    waiting-for-debugger *without* the override having been applied would
    expose the real Linux identity for its whole lifetime. Assert the
    ordering holds for every target the session attaches, regardless of count
    or type (Req 14.10).
    """
    browser = _NavigatingBrowser(targets)
    spoofer = LunaPlatformSpoofer(_CONFIG)

    # Must not raise even though service_worker targets reject the Page domain.
    await spoofer.run_attach(browser, _IDENTITY)

    for target in targets:
        # Every target — page or service worker — must be resumed and carry
        # the UA/UA-CH override (never left paused, never left on Linux UA).
        assert target.resumed is True, f"{target.session_id} never resumed"
        assert target.ua_override is True, (
            f"{target.session_id} missing UA/UA-CH override"
        )
        if target.target_type == "page":
            # Only a page target supports the Page domain, so only it gets the
            # pre-document script, and the full override must precede resume
            # (the navigation-invariance ordering invariant for pages).
            assert target.scripted is True, (
                f"{target.session_id} missing navigator/WebGL override script"
            )
            assert target.resumed_after_override is True, (
                f"{target.session_id} resumed before the override was applied"
            )
            assert target.spoofed is True
        else:
            # A service worker rejected Page.*; the script never landed, but
            # the UA override did and the target was still resumed.
            assert target.scripted is False, (
                f"non-page {target.session_id} unexpectedly scripted"
            )


def _prefix_through_page(targets: list[_Target], page_index: int) -> list[_Target]:
    """Return a FRESH target list up to and including the page_index-th page.

    Fresh ``_Target`` copies are used so each simulated navigation step starts
    from clean state (a brand-new document), mirroring that a navigation spawns
    a new target rather than reusing the previous one.
    """
    seen_pages = -1
    prefix: list[_Target] = []
    for src in targets:
        clone = _Target(src.session_id, src.target_type)
        prefix.append(clone)
        if src.target_type == "page":
            seen_pages += 1
            if seen_pages == page_index:
                break
    return prefix
