"""The service side of an Xbox PC launch: dispatch, toasts, events, exit codes.

``_dispatch_launch_kind`` must hand Xbox PC contexts to ``xbox_pc_game``
and never to the Windows (Proton/umu) path. ``run_xbox_pc_game`` turns a
refusal into a toast with its own key, brackets a spawned game with
``GAME_LAUNCHED``/``GAME_STOPPED``, and maps the exit code.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.core.types.events import Events
from unifideck.launcher.types.context import LaunchContext, RuntimeState
from unifideck.launcher.xbox_pc import session as session_mod
from unifideck.launcher.xbox_pc.errors import (
    GAME_ARGS_IGNORED_KEY,
    LAUNCH_FAILED_KEY,
    REASON_TOAST_KEYS,
    STARTING_KEY,
    XboxPcLaunchError,
    toast_key,
)
from unifideck.services.launcher import xbox_pc_game
from unifideck.services.launcher.service import LauncherService

PID = "9PK087LNGJC5"


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: Any, **kwargs: Any) -> None:
        self.events.append((str(getattr(event, "value", event)), kwargs))

    def stage_keys(self) -> list[str]:
        return [kw.get("i18n_key", "") for ev, kw in self.events if ev == Events.LAUNCHER_STAGE.value]

    def names(self) -> list[str]:
        return [ev for ev, _ in self.events]


def _svc() -> LauncherService:
    return LauncherService(bus=_Bus(), shortcut_svc=None, proton_svc=None,  # type: ignore[arg-type]
                           cloud_svc=None, edge_browser=SimpleNamespace())  # type: ignore[arg-type]


def _ctx(backend: str = "xodus", **kw: Any) -> LaunchContext:
    return LaunchContext(store="microsoft", game_id=PID, exe_path=Path("/g/Game.exe"),
                         work_dir=Path("/g"), plugin_dir=Path("/p"), launch_backend=backend, **kw)


@pytest.fixture(autouse=True)
def _no_real_world(monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.launcher.proton.infrastructure import container_escape

    monkeypatch.setattr(container_escape, "in_pressure_vessel", lambda: False)
    monkeypatch.setattr(xbox_pc_game, "build_deps",
                        lambda svc, ctx: session_mod.LaunchDeps(steam_root=Path("/steam")))


def _fake_launch(monkeypatch: pytest.MonkeyPatch, *, rc: int = 0, error: XboxPcLaunchError | None = None) -> None:
    class Fake:
        def __init__(self, ctx: LaunchContext, deps: session_mod.LaunchDeps) -> None:
            self.deps = deps

        async def run(self) -> int:
            if error is not None:
                raise error
            assert self.deps.on_spawned is not None
            await self.deps.on_spawned()
            return rc

    monkeypatch.setattr(session_mod, "XboxPcLaunch", Fake)


# ── dispatch ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(("backend", "target"), [
    ("xodus", "run_xbox_pc_game"), ("xodus_notice", "run_xbox_pc_notice"),
])
async def test_xbox_pc_contexts_never_reach_the_windows_path(
    monkeypatch: pytest.MonkeyPatch, backend: str, target: str,
) -> None:
    svc = _svc()
    called: list[str] = []

    async def spy(*_a: Any) -> Any:
        called.append(target)
        from unifideck.core.types import Result
        return Result(success=True)

    async def windows(*_a: Any) -> Any:
        raise AssertionError("launch_windows must never run for an Xbox PC title")

    monkeypatch.setattr(xbox_pc_game, target, spy)
    monkeypatch.setattr(svc, "_launch_windows", windows)
    monkeypatch.setattr(svc, "_launch_native", windows)
    ctx = _ctx(backend, notice_reason="disabled" if backend == "xodus_notice" else None)
    assert not ctx.is_windows_game
    await svc._dispatch_launch_kind(ctx, RuntimeState())
    assert called == [target]


@pytest.mark.asyncio
async def test_a_notice_shows_only_its_own_toast() -> None:
    svc = _svc()
    result = await svc._try_launch(_ctx("xodus_notice", notice_reason="not_installed"), RuntimeState())
    assert result.success is False and result.error_code == "xbox_pc_not_installed"
    bus = svc._bus
    assert isinstance(bus, _Bus)
    assert bus.stage_keys() == [REASON_TOAST_KEYS["not_installed"]]
    assert Events.GAME_LAUNCHED.value not in bus.names()


# ── run_xbox_pc_game ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_clean_run_brackets_the_game_with_launch_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_launch(monkeypatch, rc=0)
    svc = _svc()
    result = await xbox_pc_game.run_xbox_pc_game(svc, _ctx(), RuntimeState())
    assert result.success
    bus = svc._bus
    assert isinstance(bus, _Bus)
    names = bus.names()
    assert names.index(Events.GAME_LAUNCHED.value) < names.index(Events.GAME_STOPPED.value)
    assert bus.stage_keys() == [STARTING_KEY]


@pytest.mark.asyncio
async def test_a_nonzero_exit_maps_to_exit_n(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_launch(monkeypatch, rc=5)
    result = await xbox_pc_game.run_xbox_pc_game(_svc(), _ctx(), RuntimeState())
    assert result.success is False and result.error_code == "exit_5"


@pytest.mark.asyncio
async def test_a_refusal_toasts_its_key_and_emits_no_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_launch(monkeypatch, error=XboxPcLaunchError("eac_runtime_missing", "/secret/path"))
    svc = _svc()
    result = await xbox_pc_game.run_xbox_pc_game(svc, _ctx(), RuntimeState())
    assert result.error_code == "xbox_pc_eac_runtime_missing"
    bus = svc._bus
    assert isinstance(bus, _Bus)
    assert bus.stage_keys()[-1] == "toasts.launcher.xboxPcEacRuntimeMissing"
    stage = next(kw for _ev, kw in bus.events if kw.get("i18n_key") == bus.stage_keys()[-1])
    assert stage["severity"] == "error"
    assert stage["i18n_params"]["reason"] == "eac_runtime_missing"
    assert "game_key" in stage["i18n_params"]
    assert "/secret/path" not in str(stage)
    assert Events.GAME_LAUNCHED.value not in bus.names()
    assert Events.GAME_STOPPED.value not in bus.names()


@pytest.mark.asyncio
async def test_a_forced_compat_tool_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.launcher.proton.infrastructure import container_escape

    monkeypatch.setattr(container_escape, "in_pressure_vessel", lambda: True)
    _fake_launch(monkeypatch, error=AssertionError("must not run"))  # type: ignore[arg-type]
    result = await xbox_pc_game.run_xbox_pc_game(_svc(), _ctx(), RuntimeState())
    assert result.error_code == "xbox_pc_compat_tool_wrapped"


@pytest.mark.asyncio
async def test_dropped_game_args_are_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_launch(monkeypatch, rc=0)
    svc = _svc()
    await xbox_pc_game.run_xbox_pc_game(svc, _ctx(), RuntimeState(game_args=["-windowed"]))
    bus = svc._bus
    assert isinstance(bus, _Bus)
    assert GAME_ARGS_IGNORED_KEY in bus.stage_keys()


def test_every_reason_has_a_launcher_toast_key() -> None:
    assert toast_key("something_new") == LAUNCH_FAILED_KEY
    for key in REASON_TOAST_KEYS.values():
        assert key.startswith("toasts.launcher.xboxPc")


@pytest.mark.parametrize(("app_id", "expected"), [(None, None), ("0", None), ("-123456", True)])
def test_window_env_only_with_a_known_app_id(app_id: str | None, expected: Any) -> None:
    env = xbox_pc_game._window_env(_ctx(steam_app_id=app_id))
    if expected is None:
        assert env is None
    else:
        assert env is not None and env["SteamGameId"] == str(-123456 & 0xFFFFFFFF)


_REAL_BUILD_DEPS = xbox_pc_game.build_deps


def test_build_deps_resolves_every_lazy_import() -> None:
    """The lazy imports run only at launch; a typo there would ship broken."""
    deps = _REAL_BUILD_DEPS(_svc(), _ctx(steam_app_id="-123456"))
    try:
        assert deps.window_env is not None
        assert deps.language == "en-US"  # no config: the resolver default
        assert deps.fast_fail_s > 0
        assert deps.notify is not None
    finally:
        if deps.log_file is not None:
            deps.log_file.close()
