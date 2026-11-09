"""Foundation pieces the Xbox PC launch path builds on.

* ``LaunchContext.launch_backend``: an Xodus context is neither a Windows
  (umu) game nor a native one, whatever its exe is called.
* ``game_prefix_root``: the one definition of a non-wrapper game's prefix,
  now used by ``core._resolve_prefix``.
* ``browser_context`` and ``resolve_bypass_flag``, moved out of the
  dispatcher to keep it under its line cap.
* ``maybe_start_window_tagger``, extracted from ``core.proton_prepare``.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from unifideck.launcher import browser_games, launch_flags
from unifideck.launcher.browser_games import BrowserTarget
from unifideck.launcher.proton.infrastructure import core, gamescope_window_tagger, prefix_layout
from unifideck.launcher.types.context import LaunchContext


def _ctx(**kw: object) -> LaunchContext:
    base: dict[str, object] = {
        "store": "microsoft", "game_id": "9PK087LNGJC5",
        "exe_path": Path("/g/Balatro.exe"), "work_dir": Path("/g"),
        "plugin_dir": Path("/p"),
    }
    base.update(kw)
    return LaunchContext(**base)  # type: ignore[arg-type]


def test_xodus_context_is_not_routed_to_proton_or_native() -> None:
    ctx = _ctx(launch_backend="xodus")
    assert ctx.is_xbox_pc_game and not ctx.is_xbox_pc_notice
    assert ctx.is_windows_game is False
    assert ctx.is_native_linux is False
    log = ctx.to_log_dict()
    assert log["launch_backend"] == "xodus" and log["notice_reason"] is None


def test_notice_context() -> None:
    ctx = _ctx(launch_backend="xodus_notice", notice_reason="disabled",
               exe_path=Path("/dev/null"))
    assert ctx.is_xbox_pc_notice and not ctx.is_xbox_pc_game
    assert not ctx.is_windows_game and not ctx.is_native_linux
    assert ctx.to_log_dict()["notice_reason"] == "disabled"


def test_ordinary_contexts_are_unchanged() -> None:
    assert _ctx().is_windows_game is True
    assert _ctx(exe_path=Path("/g/start.sh")).is_native_linux is True
    assert _ctx().launch_backend is None


def test_game_prefix_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".local/share/unifideck/prefixes/9PK087LNGJC5"
    assert prefix_layout.game_prefix_root("9PK087LNGJC5") == expected
    assert prefix_layout.game_prefix_root("9PK087LNGJC5/pfx") == expected
    assert not expected.exists(), "building the path creates nothing"


def test_core_resolve_prefix_uses_game_prefix_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = core._resolve_prefix(_ctx(store="epic", game_id="Salt"))
    assert path == prefix_layout.game_prefix_root("Salt")
    assert path.is_dir()


def test_browser_context_moved_unchanged(tmp_path: Path) -> None:
    target = BrowserTarget(url="https://www.xbox.com/play/launch/X", kind="stream")
    ctx = browser_games.browser_context("microsoft", "X", "", target, tmp_path)
    assert ctx.browser_url == target.url and ctx.browser_kind == "stream"
    assert ctx.exe_path == Path("browser") and ctx.plugin_dir == tmp_path
    assert ctx.is_browser_game and not ctx.bypass_circuit_breaker


def test_bypass_flag_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A fake home, so the launch-history read can never see the real one.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UNIFIDECK_BYPASS_CIRCUIT_BREAKER", "yes")
    assert launch_flags.resolve_bypass_flag("epic", "Salt", tmp_path) is True
    monkeypatch.setenv("UNIFIDECK_BYPASS_CIRCUIT_BREAKER", "")
    assert launch_flags.resolve_bypass_flag("epic", "Salt", tmp_path / "missing") is False


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def fake_start(appid: int) -> threading.Event:
        calls.append(appid)
        return threading.Event()

    monkeypatch.setattr(gamescope_window_tagger, "start_window_tagger", fake_start)
    return calls


def test_tagger_starts_only_in_gamescope_with_an_app_id(
    monkeypatch: pytest.MonkeyPatch, started: list[int],
) -> None:
    tag = gamescope_window_tagger.maybe_start_window_tagger
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    assert tag({"SteamGameId": "123"}, log_tag="t") is None
    monkeypatch.setenv("GAMESCOPE_WAYLAND_DISPLAY", "gamescope-0")
    assert tag({"SteamGameId": "0"}, log_tag="t") is None
    assert tag({"SteamGameId": "junk"}, log_tag="t") is None
    assert tag({}, log_tag="t") is None
    assert isinstance(tag({"SteamGameId": "3221225472"}, log_tag="t"), threading.Event)
    assert started == [3221225472]
