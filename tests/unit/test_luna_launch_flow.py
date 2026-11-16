"""Unit tests for the Luna launch orchestration (task 7.4).

Drives ``run_luna_browser_game`` with a mocked ``LunaBrowser.launch``,
mocked spoofer/activator/keyboard, and a fake ``LauncherService``, so the
branch selection in ``run_browser_game`` and the error-mapping of the Luna
sequence are exercised without a real browser. The full integration drive
against a live headless browser is task 7.5.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.stores.luna import luna_launch_flow as flow
from unifideck.stores.luna.luna_launch_flow import (
    ERR_EDGE_LAUNCH_FAILED,
    ERR_LAUNCH_FAILURE,
    ERR_MISSING_CDP_DEPENDENCY,
    ERR_PLATFORM_SPOOFING_FAILURE,
    ERR_XAUTHORITY_NOT_FOUND,
    run_luna_browser_game,
)
from unifideck.stores.luna.luna_platform_spoofer import LunaSpoofConfigError
from unifideck.stores.luna.luna_play_activator import (
    ActivationResult,
    LunaPlayActivationError,
)

_WINDOWS = "Windows"


def _ctx() -> Any:
    return SimpleNamespace(
        store="luna",
        game_id="B0HLN4GG5K",
        game_key="luna:B0HLN4GG5K",
        browser_url="https://luna.amazon.de/detail/B0HLN4GG5K",
        browser_kind="stream",
    )


def _svc(*, installed: bool = True) -> Any:
    events: list[tuple[Any, ...]] = []

    async def emit(*a: Any, **k: Any) -> None:
        events.append((a, k))

    return SimpleNamespace(
        _config=None,
        _bus=SimpleNamespace(emit=emit),
        _edge_browser=SimpleNamespace(
            is_installed=installed,
            process=SimpleNamespace(pid=4321),
            browser_game_cdp_port=lambda: 9223,
            locale_fn=lambda: "en-US",
        ),
        _active_subprocess=None,
        _events=events,
    )


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the Luna sequence collaborators; return knobs to vary outcomes."""
    knobs = SimpleNamespace(
        launched=True,
        attach_error=None,
        verify_value=_WINDOWS,
        verify_error=None,
        activate_result=ActivationResult.ok(),
        activate_error=None,
        keyboard_raises=None,
        keyboard_returns=True,
        waited=False,
        spoofer_closed=False,
        config=SimpleNamespace(
            spoof_uach_platform=_WINDOWS, player_path="/player", luna_host="luna.amazon.de",
            video_quality="",
            locale="",
        ),
    )

    # XAUTHORITY present by default so the happy path proceeds.
    monkeypatch.setattr(flow, "clean_env", lambda: {"XAUTHORITY": "/run/user/1000/xauth_x"})
    monkeypatch.setattr(flow, "_xauthority_present", lambda env: True)
    monkeypatch.setattr(flow.LunaConfig, "from_config_manager", staticmethod(lambda _c: knobs.config))

    class _Browser:
        def __init__(self, config: Any, edge: Any = None) -> None:
            self.config = config

        def launch(self, url: str) -> bool:
            return knobs.launched

    monkeypatch.setattr(flow, "LunaBrowser", _Browser)

    class _Spoofer:
        def __init__(self, config: Any) -> None:
            pass

        async def attach(self, cdp: Any) -> None:
            if knobs.attach_error:
                raise knobs.attach_error

        async def verify(self, cdp: Any) -> str:
            if knobs.verify_error:
                raise knobs.verify_error
            return knobs.verify_value

        async def close(self) -> None:
            knobs.spoofer_closed = True

    monkeypatch.setattr(flow, "LunaPlatformSpoofer", _Spoofer)

    class _Activator:
        def __init__(self, config: Any) -> None:
            pass

        async def activate(self, cdp: Any, detail: str, player: str) -> ActivationResult:
            if knobs.activate_error:
                raise knobs.activate_error
            return knobs.activate_result

    monkeypatch.setattr(flow, "LunaPlayActivator", _Activator)

    async def _kbd(port: int, locale: Any, **_k: Any) -> bool:
        if knobs.keyboard_raises:
            raise knobs.keyboard_raises
        return knobs.keyboard_returns

    monkeypatch.setattr(flow, "inject_virtual_keyboard", _kbd)

    async def _wait(*_a: Any, **_k: Any) -> None:
        knobs.waited = True

    monkeypatch.setattr(flow, "wait_for_browser_exit", _wait)
    monkeypatch.setattr(
        flow, "EdgeCDPClient",
        lambda cdp_port: SimpleNamespace(cdp_port=cdp_port, probe_cdp=lambda: True),
    )
    return knobs


async def test_happy_path_runs_full_sequence_and_blocks(wire: Any) -> None:
    svc = _svc()
    result = await run_luna_browser_game(svc, _ctx())
    assert result.success is True and result.store == "luna"
    assert wire.waited is True
    # Active subprocess is registered (so Stop/SIGTERM reaches it) then cleared.
    assert svc._active_subprocess is None


async def test_dispatch_from_run_browser_game_routes_luna(monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.services.launcher import browser_game as bg

    called: list[Any] = []

    async def _fake(svc: Any, ctx: Any) -> Any:
        called.append(ctx)
        return SimpleNamespace(success=True, store="luna")

    monkeypatch.setattr(
        "unifideck.stores.luna.luna_launch_flow.run_luna_browser_game", _fake,
    )
    result = await bg.run_browser_game(_svc(), _ctx())
    assert result.success is True and len(called) == 1


async def test_dispatch_refuses_luna_when_browser_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Luna runs in the shared plugin browser, so the generic missing-browser
    # abort applies before its own orchestration is reached.
    from unifideck.services.launcher import browser_game as bg

    async def _never(svc: Any, ctx: Any) -> Any:
        raise AssertionError("Luna flow must not run without a browser")

    monkeypatch.setattr(
        "unifideck.stores.luna.luna_launch_flow.run_luna_browser_game", _never,
    )
    result = await bg.run_browser_game(_svc(installed=False), _ctx())
    assert result.success is False and result.error == "edge_not_installed"


async def test_xauthority_missing_aborts_before_spawn(wire: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # No XAUTHORITY file AND no Wayland display → nothing to reach a display → abort.
    monkeypatch.setattr(flow, "clean_env", lambda: {})
    monkeypatch.setattr(flow, "_xauthority_present", lambda env: False)
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_XAUTHORITY_NOT_FOUND


async def test_wayland_display_without_xauthority_does_not_abort(wire: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Gaming Mode on the Deck: no XAUTHORITY file but WAYLAND_DISPLAY=gamescope-0
    # is present, so the launch proceeds past the display gate (mirrors xCloud/itch).
    monkeypatch.setattr(flow, "clean_env", lambda: {"WAYLAND_DISPLAY": "gamescope-0"})
    monkeypatch.setattr(flow, "_xauthority_present", lambda env: False)
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is True and result.error != ERR_XAUTHORITY_NOT_FOUND


async def test_no_supported_browser_maps_to_edge_launch_failed(wire: Any) -> None:
    wire.launched = False
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_EDGE_LAUNCH_FAILED


async def test_cdp_attach_failure_maps_to_platform_spoofing_failure(wire: Any) -> None:
    wire.attach_error = LunaSpoofConfigError("", "no browser ws")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_PLATFORM_SPOOFING_FAILURE


async def test_undeterminable_version_maps_to_platform_spoofing_failure(wire: Any) -> None:
    wire.attach_error = LunaSpoofConfigError("garbage/x", "unrecognised version")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_PLATFORM_SPOOFING_FAILURE


async def test_platform_mismatch_aborts(wire: Any) -> None:
    wire.verify_value = "Linux"
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_PLATFORM_SPOOFING_FAILURE


async def test_unreadable_verify_probe_maps_to_platform_spoofing_failure(wire: Any) -> None:
    wire.verify_error = LunaSpoofConfigError("", "probe unreadable")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_PLATFORM_SPOOFING_FAILURE


async def test_play_activation_failure_maps_to_launch_failure(wire: Any) -> None:
    wire.activate_result = ActivationResult.failed("did not reach /player")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_LAUNCH_FAILURE


async def test_play_activation_structural_error_maps_to_launch_failure(wire: Any) -> None:
    wire.activate_error = LunaPlayActivationError("no page target")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_LAUNCH_FAILURE


async def test_missing_cdp_dependency_for_keyboard_aborts(wire: Any) -> None:
    wire.keyboard_raises = ImportError("No module named 'aiohttp'")
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and result.error == ERR_MISSING_CDP_DEPENDENCY


async def test_keyboard_not_landing_is_non_fatal(wire: Any) -> None:
    wire.keyboard_returns = False
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is True and wire.waited is True


async def test_spoofer_held_until_window_closes(wire: Any) -> None:
    # Chrome drops a CDP session's overrides when it detaches, so the
    # spoofer is released only after the stream window is gone.
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is True and wire.waited is True
    assert wire.spoofer_closed is True


async def test_spoofer_released_on_platform_mismatch(wire: Any) -> None:
    wire.verify_value = "Linux"
    result = await run_luna_browser_game(_svc(), _ctx())
    assert result.success is False and wire.spoofer_closed is True


async def test_await_cdp_ready_returns_true_when_endpoint_answers() -> None:
    # probe_cdp answering immediately returns True without waiting out the timeout.
    cdp = SimpleNamespace(probe_cdp=lambda: True)
    ready = await flow._await_cdp_ready(cdp, timeout_s=0.05, interval_s=0.01)
    assert ready is True


async def test_await_cdp_ready_returns_false_after_timeout() -> None:
    # probe_cdp never answering returns False once the (tiny) timeout elapses.
    cdp = SimpleNamespace(probe_cdp=lambda: False)
    ready = await flow._await_cdp_ready(cdp, timeout_s=0.05, interval_s=0.01)
    assert ready is False
