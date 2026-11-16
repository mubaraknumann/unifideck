"""Integration / example tests for the Luna launch flow (task 7.5).

Where ``test_luna_launch_flow.py`` (task 7.4) stubbed the spoofer /
activator / keyboard as whole objects to pin the launch flow's *error
mapping*, this module drives the **real** collaborator cores
(``LunaPlatformSpoofer.run_attach`` / ``run_verify``,
``LunaPlayActivator.run_activate``, the keyboard script builder) against a
single **scripted fake CDP session** — the no-real-browser analogue of the
local harness page the design calls for. The fake session models a realistic
Luna session: a page target that attaches in flatten mode, a play control
that clicks, a detail → ``/player`` transition and a ``<video>`` that reaches
``readyState === 4 && !paused`` (the verified F7 state), so the whole
spoof → verify → activate → keyboard → block sequence runs end to end
without a browser or Amazon.

Both the ``run_*`` cores (driven directly against the fake) and the
end-to-end ``run_luna_browser_game`` orchestration (with the one CDP I/O seam
— ``_AiohttpBrowserCdpSession`` + ``EdgeCDPClient`` — swapped for the fake)
are exercised, plus the error/abort examples the task lists: play-activation
failure (5.5), reached ``/player`` but video not playing (5.4/5.5), no
supported browser (5.7), platform mismatch (5.9/14.12), undeterminable
browser version (5.10/14.9), XAUTHORITY missing (5.13), no page target
(14.13) and missing CDP dependency for the keyboard inject (7.5).

_Requirements: 5.2, 5.4, 5.5, 5.7, 7.1, 7.5, 14.12, 14.13_
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.stores.luna import luna_launch_flow as flow
from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_keyboard import build_virtual_keyboard_script
from unifideck.stores.luna.luna_launch_flow import (
    ERR_EDGE_LAUNCH_FAILED,
    ERR_LAUNCH_FAILURE,
    ERR_MISSING_CDP_DEPENDENCY,
    ERR_PLATFORM_SPOOFING_FAILURE,
    ERR_XAUTHORITY_NOT_FOUND,
    run_luna_browser_game,
)
from unifideck.stores.luna.luna_platform_spoofer import (
    LunaPlatformSpoofer,
    LunaSpoofConfigError,
    build_spoof_identity,
)
from unifideck.stores.luna.luna_play_activator import (
    LunaPlayActivationError,
    LunaPlayActivator,
)

_WINDOWS = "Windows"
_DETAIL_URL = "https://luna.amazon.de/detail/B0HLN4GG5K"
_PLAYER_PATH = "/player"
_BROWSER_FIELD = "Chrome/150.0.7871.186"

_UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
)


def _config() -> LunaConfig:
    """A fully-populated Luna config matching the design's spoof fields."""
    return LunaConfig(
        luna_host="luna.amazon.de",
        allowed_stream_hosts=["luna.amazon.de"],
        signin_path="/signin",
        player_path=_PLAYER_PATH,
        profile_dir="/home/deck/.local/share/unifideck/luna",
        browser_game_cdp_flavour="browser_game",
        spoof_ua_template=_UA_TEMPLATE,
        spoof_uach_platform=_WINDOWS,
        spoof_navigator_platform="Win32",
        spoof_webgl_renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX, Direct3D11)",
    )


# ── The scripted fake CDP session (the "harness") ───────────────────
#
# One mock implementing the BrowserCdpSession protocol the spoofer and
# activator both drive: Target.getTargets / attachToTarget / setAutoAttach
# and per-target Runtime.evaluate. It records the order of commands per
# session so the "override applied before resume" invariant (14.1-14.7) and
# the detail → /player progression (5.4) are observable, and models a video
# that only reaches readyState 4 after the play control is clicked.


class FakeLunaSession:
    """A scripted browser-level CDP session modelling a healthy Luna launch.

    Models a single ``page`` target. Platform probes answer the configured
    Windows platform (the spoof "took"), the play control clicks on the first
    evaluate, and the player confirmation flips to ``onPlayer``/``playing``
    once play has been triggered — reproducing the real detail → ``/player``
    + ``<video>`` playing progression against which the real ``run_*`` cores
    are driven.
    """

    PAGE_SESSION = "sid-page-1"

    def __init__(
        self,
        *,
        reported_platform: str = _WINDOWS,
        video_plays: bool = True,
        has_page_target: bool = True,
        extra_targets: list[dict[str, Any]] | None = None,
    ) -> None:
        self.reported_platform = reported_platform
        self.video_plays = video_plays
        self.has_page_target = has_page_target
        self._extra_targets = extra_targets or []
        self._play_triggered = False
        # Per-session ordered command log, keyed by sessionId (None = browser).
        self.commands: dict[str | None, list[str]] = {}
        self.closed = False

    def _record(self, session_id: str | None, method: str) -> None:
        self.commands.setdefault(session_id, []).append(method)

    def _targets(self) -> list[dict[str, Any]]:
        page = (
            [{"type": "page", "targetId": "T-page", "url": _DETAIL_URL}]
            if self.has_page_target
            else []
        )
        return page + self._extra_targets

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self._record(session_id, method)
        params = params or {}
        if method == "Target.getTargets":
            return {"targetInfos": self._targets()}
        if method == "Target.attachToTarget":
            return {"sessionId": self.PAGE_SESSION}
        if method == "Target.setAutoAttach":
            return {}
        if method == "Runtime.evaluate":
            return self._evaluate(params.get("expression", ""))
        # A trusted pointer release on the located play button starts play.
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            self._play_triggered = True
        return {}

    def _evaluate(self, expression: str) -> dict[str, Any]:
        # Platform probe (spoofer.verify): report the configured platform.
        if "getHighEntropyValues" in expression:
            return {"result": {"type": "string", "value": self.reported_platform}}
        # Player confirmation probe (activator): object with onPlayer/playing.
        if "onPlayer" in expression:
            on_player = self._play_triggered
            playing = self._play_triggered and self.video_plays
            return {
                "result": {
                    "type": "object",
                    "value": {"onPlayer": on_player, "playing": playing},
                },
            }
        # Play-button locate (activator trigger): the button's centre.
        if "primary_entitled_button_game_session_async" in expression:
            return {"result": {"type": "object", "value": [194, 356]}}
        # Pre-launch dialog locate: no dialog in this harness.
        if "continue anyway" in expression:
            return {"result": {"type": "object", "value": None}}
        # The injected spoof override script (addScriptToEvaluateOnNewDocument
        # re-run against the current document) — no return value needed.
        return {"result": {"type": "undefined"}}

    async def attached_targets(self) -> Any:
        for target in self._targets():
            yield self.PAGE_SESSION, target

    async def close(self) -> None:
        self.closed = True


def _identity(config: LunaConfig) -> Any:
    return build_spoof_identity(config, _BROWSER_FIELD)


# ──────────────────────────────────────────────────────────────────
# Part A — the real collaborator cores against the harness session
# ──────────────────────────────────────────────────────────────────


async def test_spoofer_applies_full_override_before_resume() -> None:
    """run_attach applies the UA + script override before resuming (14.1-14.7)."""
    config = _config()
    session = FakeLunaSession()
    spoofer = LunaPlatformSpoofer(config)

    await spoofer.run_attach(session, _identity(config))

    # Browser-level auto-attach was installed first.
    assert session.commands[None] == ["Target.setAutoAttach"]
    page_cmds = session.commands[FakeLunaSession.PAGE_SESSION]
    # The full override (UA + pre-document script) is applied, and
    # runIfWaitingForDebugger comes AFTER all of it (the key invariant).
    assert "Emulation.setUserAgentOverride" in page_cmds
    assert "Page.addScriptToEvaluateOnNewDocument" in page_cmds
    resume_idx = page_cmds.index("Runtime.runIfWaitingForDebugger")
    assert page_cmds.index("Emulation.setUserAgentOverride") < resume_idx
    assert page_cmds.index("Page.addScriptToEvaluateOnNewDocument") < resume_idx


async def test_spoofer_verify_reports_windows_on_spoofed_session() -> None:
    """run_verify returns the Windows platform the harness reports (14.11)."""
    session = FakeLunaSession(reported_platform=_WINDOWS)
    reported = await LunaPlatformSpoofer(_config()).run_verify(session)
    assert reported == _WINDOWS


async def test_activator_confirms_player_and_video_playing() -> None:
    """run_activate reaches /player with the video playing (5.4, F7 state)."""
    session = FakeLunaSession(video_plays=True)
    result = await LunaPlayActivator(_config()).run_activate(
        session, FakeLunaSession.PAGE_SESSION, _DETAIL_URL, _PLAYER_PATH,
    )
    assert result.success is True
    assert result.reached_player is True


async def test_keyboard_script_builds_qwerty_for_non_fr_locale() -> None:
    """The reused keyboard inject builds a well-formed QWERTY script (7.1)."""
    qwerty = build_virtual_keyboard_script("en-US")
    azerty = build_virtual_keyboard_script("fr-FR")
    assert "unifideck-luna-keyboard" in qwerty
    assert "installed:" in qwerty
    # Rows are JSON-encoded as per-key arrays; the QWERTY row distinguishes
    # the layouts. The non-fr locale must not pick up the AZERTY top row.
    assert '"q", "w", "e", "r", "t", "y"' in qwerty
    assert '"q", "w", "e", "r", "t", "y"' not in azerty
    assert '"a", "z", "e", "r", "t", "y"' in azerty


# ──────────────────────────────────────────────────────────────────
# Part B — the full end-to-end flow with the single CDP I/O seam faked
# ──────────────────────────────────────────────────────────────────


class _FakeCDPClient:
    """Stand-in for EdgeCDPClient: hands out the harness over its ws seam."""

    def __init__(self, cdp_port: int, browser_field: str = _BROWSER_FIELD) -> None:
        self.cdp_port = cdp_port
        self._browser_field = browser_field

    def get_browser_version(self) -> str | None:
        return self._browser_field

    def get_browser_ws_url(self) -> str | None:
        return f"ws://127.0.0.1:{self.cdp_port}/devtools/browser/abc"

    def probe_cdp(self) -> bool:
        return True


def _svc() -> Any:
    events: list[tuple[Any, ...]] = []

    async def emit(*a: Any, **k: Any) -> None:
        events.append((a, k))

    return SimpleNamespace(
        _config=None,
        _bus=SimpleNamespace(emit=emit),
        _edge_browser=SimpleNamespace(
            process=SimpleNamespace(pid=4321, wait=lambda: 0),
            browser_game_cdp_port=lambda: 9223,
            locale_fn=lambda: "en-US",
        ),
        _active_subprocess=None,
        _events=events,
    )


def _ctx() -> Any:
    return SimpleNamespace(
        store="luna",
        game_id="B0HLN4GG5K",
        game_key="luna:B0HLN4GG5K",
        browser_url=_DETAIL_URL,
        browser_kind="stream",
    )


@pytest.fixture()
def wire_end_to_end(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire the real launch flow over the scripted harness session.

    Only the three seams a real browser would own are faked: the kiosk
    *spawn* (``LunaBrowser.launch``), the CDP *client* (``EdgeCDPClient`` →
    the ws URL + browser version), the CDP *transport* used by the spoofer /
    activator (``_AiohttpBrowserCdpSession`` → :class:`FakeLunaSession`), the
    keyboard inject's CDP page-inject, and the blocking ``wait_for_browser_exit``.
    Everything else — ``run_attach`` / ``run_verify`` / ``run_activate`` and
    the launch-flow sequencing — is the real code.
    """
    knobs = SimpleNamespace(
        launched=True,
        session=FakeLunaSession(),
        browser_field=_BROWSER_FIELD,
        keyboard_returns=True,
        keyboard_raises=None,
        waited=False,
        xauth_present=True,
        config=_config(),
    )

    # When simulating the no-display case (xauth_present False) clean_env must
    # also carry no WAYLAND_DISPLAY, so the missing-display abort still fires;
    # otherwise a resolved XAUTHORITY path is enough for the gate.
    def _clean_env() -> dict[str, str]:
        if knobs.xauth_present:
            return {"XAUTHORITY": "/run/user/1000/xauth_x"}
        return {}

    monkeypatch.setattr(flow, "clean_env", _clean_env)
    monkeypatch.setattr(flow, "_xauthority_present", lambda env: knobs.xauth_present)
    monkeypatch.setattr(
        flow.LunaConfig, "from_config_manager", staticmethod(lambda _c: knobs.config),
    )

    class _Browser:
        def __init__(self, config: Any, edge: Any = None) -> None:
            self.config = config

        def launch(self, url: str) -> bool:
            return knobs.launched

    monkeypatch.setattr(flow, "LunaBrowser", _Browser)
    monkeypatch.setattr(
        flow, "EdgeCDPClient",
        lambda cdp_port: _FakeCDPClient(cdp_port, knobs.browser_field),
    )

    # The spoofer and activator both open an _AiohttpBrowserCdpSession from a
    # ws URL; hand them the scripted harness instead so their real run_* cores
    # drive it. Patch at the module each imports it into.
    def _fake_session_factory(_ws_url: str) -> FakeLunaSession:
        return knobs.session

    import unifideck.stores.luna.luna_platform_spoofer as spoofer_mod
    import unifideck.stores.luna.luna_play_activator as activator_mod

    monkeypatch.setattr(spoofer_mod, "_AiohttpBrowserCdpSession", _fake_session_factory)
    monkeypatch.setattr(activator_mod, "_AiohttpBrowserCdpSession", _fake_session_factory)

    async def _kbd(port: int, locale: Any, **_k: Any) -> bool:
        if knobs.keyboard_raises:
            raise knobs.keyboard_raises
        return knobs.keyboard_returns

    monkeypatch.setattr(flow, "inject_virtual_keyboard", _kbd)

    async def _wait(*_a: Any, **_k: Any) -> None:
        knobs.waited = True

    monkeypatch.setattr(flow, "wait_for_browser_exit", _wait)
    return knobs


async def test_end_to_end_happy_path_reaches_player(wire_end_to_end: Any) -> None:
    """Full flow: spawn → spoof → verify → activate → keyboard → block (5.2, 5.4)."""
    svc = _svc()
    result = await run_luna_browser_game(svc, _ctx())

    assert result.success is True and result.store == "luna"
    # The real activator confirmed the player over the harness.
    assert wire_end_to_end.session._play_triggered is True
    # The spoofer attached browser-level and the override preceded the resume.
    page_cmds = wire_end_to_end.session.commands[FakeLunaSession.PAGE_SESSION]
    assert "Emulation.setUserAgentOverride" in page_cmds
    assert "Runtime.runIfWaitingForDebugger" in page_cmds
    # Blocked on the shared wait helper, then cleaned up the subprocess handle.
    assert wire_end_to_end.waited is True
    assert svc._active_subprocess is None


async def test_end_to_end_no_supported_browser_aborts(wire_end_to_end: Any) -> None:
    """No supported browser → edge_launch_failed, no CDP work (5.7)."""
    wire_end_to_end.launched = False
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_EDGE_LAUNCH_FAILED
    # Never touched the CDP session.
    assert wire_end_to_end.session.commands == {}


async def test_end_to_end_xauthority_missing_aborts_before_spawn(wire_end_to_end: Any) -> None:
    """No XAUTHORITY file → xauthority_not_found before anything opens (5.13)."""
    wire_end_to_end.xauth_present = False
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_XAUTHORITY_NOT_FOUND
    assert wire_end_to_end.session.commands == {}


async def test_end_to_end_platform_mismatch_aborts(wire_end_to_end: Any) -> None:
    """Spoof verify reports Linux → platform_spoofing_failure (5.9, 14.12)."""
    wire_end_to_end.session = FakeLunaSession(reported_platform="Linux")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_PLATFORM_SPOOFING_FAILURE


async def test_end_to_end_undeterminable_version_aborts(wire_end_to_end: Any) -> None:
    """Browser reports no version → platform_spoofing_failure (5.10, 14.9)."""
    wire_end_to_end.browser_field = ""
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_PLATFORM_SPOOFING_FAILURE


async def test_end_to_end_no_page_target_aborts(wire_end_to_end: Any) -> None:
    """No page target to verify against → platform_spoofing_failure (14.13)."""
    wire_end_to_end.session = FakeLunaSession(has_page_target=False)
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_PLATFORM_SPOOFING_FAILURE


async def test_end_to_end_video_never_plays_maps_to_launch_failure(wire_end_to_end: Any) -> None:
    """Reached /player but <video> never plays → launch_failure (5.4, 5.5).

    Shrinks the activator's 30s player-confirm window to a tick so the
    integration test does not actually wait 30s for the timeout.
    """
    import unifideck.stores.luna.luna_play_activator as activator_mod

    wire_end_to_end.session = FakeLunaSession(video_plays=False)
    monkeypatch_short = pytest.MonkeyPatch()
    monkeypatch_short.setattr(activator_mod, "_PLAYER_CONFIRM_TIMEOUT_S", 0.01)
    monkeypatch_short.setattr(activator_mod, "_CONFIRM_POLL_INTERVAL_S", 0.0)
    try:
        result = await run_luna_browser_game(_svc(), _ctx())
    finally:
        monkeypatch_short.undo()
    assert result.success is False and result.error == ERR_LAUNCH_FAILURE


async def test_end_to_end_play_control_absent_maps_to_launch_failure(wire_end_to_end: Any) -> None:
    """Play control never clickable → launch_failure (5.5).

    A session whose play-button locate never finds the button never arms
    the player, so the 15s trigger window times out; it is shrunk to a tick.
    """
    import unifideck.stores.luna.luna_play_activator as activator_mod

    class _NoPlayControl(FakeLunaSession):
        def _evaluate(self, expression: str) -> dict[str, Any]:
            if "primary_entitled_button_game_session_async" in expression:
                return {"result": {"type": "object", "value": None}}
            return super()._evaluate(expression)

    wire_end_to_end.session = _NoPlayControl()
    mp = pytest.MonkeyPatch()
    mp.setattr(activator_mod, "_PLAY_TRIGGER_TIMEOUT_S", 0.01)
    mp.setattr(activator_mod, "_CONFIRM_POLL_INTERVAL_S", 0.0)
    try:
        result = await run_luna_browser_game(_svc(), _ctx())
    finally:
        mp.undo()
    assert result.success is False and result.error == ERR_LAUNCH_FAILURE


async def test_end_to_end_missing_cdp_dependency_for_keyboard_aborts(wire_end_to_end: Any) -> None:
    """Missing CDP dep on keyboard inject → missing_cdp_dependency (7.5)."""
    wire_end_to_end.keyboard_raises = ImportError("No module named 'aiohttp'")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_MISSING_CDP_DEPENDENCY


async def test_end_to_end_keyboard_not_landing_is_non_fatal(wire_end_to_end: Any) -> None:
    """A keyboard that does not land is logged but the launch still succeeds."""
    wire_end_to_end.keyboard_returns = False
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is True and wire_end_to_end.waited is True


# ──────────────────────────────────────────────────────────────────
# Part C — the structural-failure vs launch-failure distinction
# ──────────────────────────────────────────────────────────────────


async def test_activator_raises_structural_error_when_no_page_target() -> None:
    """activate() with no page target raises (not an ActivationResult)."""
    session = FakeLunaSession(has_page_target=False)

    activator = LunaPlayActivator(_config())
    with pytest.raises(LunaPlayActivationError):
        await activator._attach_to_page_target(session)


async def test_spoofer_verify_unreadable_probe_raises() -> None:
    """A verify probe that returns no value raises LunaSpoofConfigError."""
    class _BadProbe(FakeLunaSession):
        def _evaluate(self, expression: str) -> dict[str, Any]:
            if "getHighEntropyValues" in expression:
                return {"result": {"type": "undefined"}}
            return super()._evaluate(expression)

    with pytest.raises(LunaSpoofConfigError):
        await LunaPlatformSpoofer(_config()).run_verify(_BadProbe())
