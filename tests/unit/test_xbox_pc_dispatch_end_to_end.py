"""An installed Xbox PC game never reaches Proton, umu or the exe guess.

The ``games.map`` row of an Xbox PC install points at a Windows ``.exe``,
which is exactly what the Windows path (umu, GE-Proton, ``setup_prefix``)
would pick up. With the toggle on, the dispatcher must hand it to the Xodus
runtime instead (I-017). This runs the real ``_build_context`` and the real
``_dispatch_launch_kind`` together, with only the game process faked.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.launcher import browser_games
from unifideck.launcher import dispatcher as d
from unifideck.launcher.types.context import RuntimeState
from unifideck.launcher.xbox_pc import routing
from unifideck.launcher.xbox_pc import session as session_mod
from unifideck.services.launcher import orchestrator, xbox_pc_game
from unifideck.services.launcher.service import LauncherService

PID = "9PK087LNGJC5"
ENTRY = SimpleNamespace(exe=f"/games/Xbox/{PID}/game/Balatro.exe",
                        work_dir=f"/games/Xbox/{PID}/game", app_id=-123456)


class _Config:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def get_bool(self, key: str, default: bool = False) -> bool:
        return self.enabled if key == "stores.microsoft.xbox_pc.enabled" else default


class _Shortcuts:
    async def get_entry_for_game_key(self, store: str, game_id: str) -> Any:
        return ENTRY


class _Bus:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def emit(self, event: Any, **_kw: Any) -> None:
        self.names.append(str(getattr(event, "value", event)))


def _never(name: str) -> Any:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"{name} must never run for an installed Xbox PC title")
    return boom


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    from unifideck.launcher.proton.infrastructure import container_escape

    for store in ("EPIC", "GOG", "AMAZON", "MICROSOFT", "UBISOFT", "BATTLENET", "ITCH"):
        monkeypatch.delenv(f"UNIFIDECK_{store}_ACTION", raising=False)
    cache = {"libraries": {"microsoft": [
        {"store_game_id": PID, "tags": ["xcloud", "browser", "xbox_pc"], "metadata": {}},
    ]}}
    (tmp_path / "library_cache.json").write_text(json.dumps(cache))
    monkeypatch.setattr(browser_games, "LIBRARY_CACHE", str(tmp_path / "library_cache.json"))
    monkeypatch.setattr(d, "_resolve_plugin_dir", lambda: tmp_path)
    monkeypatch.setattr(routing, "resolve_bypass_flag", lambda *a: False)
    monkeypatch.setattr(d, "_resolve_exe_from_install", _never("the exe repair guess"))
    monkeypatch.setattr(d, "_resolve_game_exe", _never("_resolve_game_exe"))
    monkeypatch.setattr(orchestrator, "launch_windows", _never("launch_windows (umu/Proton)"))
    monkeypatch.setattr(orchestrator, "launch_native", _never("launch_native"))
    monkeypatch.setattr(container_escape, "in_pressure_vessel", lambda: False)
    monkeypatch.setattr(xbox_pc_game, "build_deps",
                        lambda svc, ctx: session_mod.LaunchDeps(steam_root=Path("/steam")))
    runs: list[str] = []

    class FakeLaunch:
        def __init__(self, ctx: Any, deps: session_mod.LaunchDeps) -> None:
            self.ctx, self.deps = ctx, deps

        async def run(self) -> int:
            runs.append(self.ctx.game_id)
            assert self.deps.on_spawned is not None
            await self.deps.on_spawned()
            return 0

    monkeypatch.setattr(session_mod, "XboxPcLaunch", FakeLaunch)
    return runs


@pytest.mark.asyncio
async def test_an_installed_exe_row_runs_through_xodus_only(_hermetic: list[str]) -> None:
    ctx = await d._build_context(["launcher", f"microsoft:{PID}"], _Shortcuts(), _Config(True))
    assert ctx.is_xbox_pc_game and not ctx.is_windows_game and not ctx.is_native_linux
    bus = _Bus()
    svc = LauncherService(bus=bus, shortcut_svc=None, proton_svc=None,  # type: ignore[arg-type]
                          cloud_svc=None, edge_browser=SimpleNamespace())  # type: ignore[arg-type]
    result = await svc._dispatch_launch_kind(ctx, RuntimeState())
    assert result.success is True
    assert _hermetic == [PID]
    assert "game_launched" in bus.names


@pytest.mark.asyncio
async def test_with_the_toggle_off_the_same_row_streams(_hermetic: list[str]) -> None:
    ctx = await d._build_context(["launcher", f"microsoft:{PID}"], _Shortcuts(), _Config(False))
    assert ctx.is_browser_game and not ctx.is_windows_game
    assert _hermetic == []
