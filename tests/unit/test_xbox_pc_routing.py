"""Where a Microsoft title's Play goes (``launcher/xbox_pc/routing.py``).

The table: an installed title with the toggle on runs through Xodus; an
xCloud title otherwise streams; a PC-only title gets a notice and never
opens a stream. Microsoft must never reach the exe repair guess, and the
auth, storefront and Xbox PC sign-in actions are untouched by the routing.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.launcher import browser_games
from unifideck.launcher import dispatcher as d
from unifideck.launcher.xbox_pc import routing

PID = "9PK087LNGJC5"


class _Config:
    def __init__(self, enabled: Any) -> None:
        self._enabled = enabled

    def get_bool(self, key: str, default: bool = False) -> bool:
        assert key == "stores.microsoft.xbox_pc.enabled"
        if isinstance(self._enabled, Exception):
            raise self._enabled
        return bool(self._enabled)


class _Shortcuts:
    def __init__(self, entry: Any) -> None:
        self.entry = entry

    async def get_entry_for_game_key(self, store: str, game_id: str) -> Any:
        assert store == "microsoft"
        return self.entry


_ENTRY = SimpleNamespace(exe=f"/games/Xbox/{PID}/game/Balatro.exe",
                         work_dir=f"/games/Xbox/{PID}/game", app_id=-123456)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for store in ("EPIC", "GOG", "AMAZON", "MICROSOFT", "UBISOFT", "BATTLENET", "ITCH"):
        monkeypatch.delenv(f"UNIFIDECK_{store}_ACTION", raising=False)
    monkeypatch.setattr(browser_games, "LIBRARY_CACHE", str(tmp_path / "library_cache.json"))
    monkeypatch.setattr(d, "_resolve_plugin_dir", lambda: tmp_path)
    monkeypatch.setattr(routing, "resolve_bypass_flag", lambda *a: False)

    def _no_exe_guess(*_a: Any, **_k: Any) -> None:
        raise AssertionError("Microsoft must never reach the exe repair guess")

    monkeypatch.setattr(d, "_resolve_exe_from_install", _no_exe_guess)


def _cache(tmp_path: Path, tags: list[str] | None) -> None:
    """Write the library cache; ``None`` means no record for the title."""
    games = [] if tags is None else [{"store_game_id": PID, "tags": tags, "metadata": {}}]
    (tmp_path / "library_cache.json").write_text(json.dumps({"libraries": {"microsoft": games}}))


async def _route(entry: Any, enabled: Any, tmp_path: Path) -> Any:
    return await d._build_context(
        ["launcher", f"microsoft:{PID}"], _Shortcuts(entry), _Config(enabled),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", [["xcloud", "browser", "xbox_pc"], ["xbox_pc"], None])
async def test_installed_with_the_toggle_on_runs_through_xodus(tmp_path: Path, tags: Any) -> None:
    _cache(tmp_path, tags)
    ctx = await _route(_ENTRY, True, tmp_path)
    assert ctx.is_xbox_pc_game and not ctx.is_windows_game and not ctx.is_browser_game
    assert ctx.exe_path == Path(_ENTRY.exe)
    assert ctx.steam_app_id == "-123456"


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", [["xcloud", "browser", "xbox_pc"], None])
async def test_installed_with_the_toggle_off_streams_when_xcloud_has_it(tmp_path: Path, tags: Any) -> None:
    _cache(tmp_path, tags)
    ctx = await _route(_ENTRY, False, tmp_path)
    assert ctx.is_browser_game and ctx.browser_kind == "stream"
    assert ctx.browser_url == f"https://www.xbox.com/play/launch/{PID}"


@pytest.mark.asyncio
async def test_installed_pc_only_with_the_toggle_off_is_a_disabled_notice(tmp_path: Path) -> None:
    _cache(tmp_path, ["xbox_pc"])
    ctx = await _route(_ENTRY, False, tmp_path)
    assert ctx.is_xbox_pc_notice and ctx.notice_reason == "disabled"
    assert not ctx.is_browser_game and not ctx.is_windows_game and not ctx.is_native_linux


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_not_installed_xcloud_title_streams(tmp_path: Path, enabled: bool) -> None:
    _cache(tmp_path, ["xcloud", "browser"])
    ctx = await _route(None, enabled, tmp_path)
    assert ctx.is_browser_game and ctx.browser_kind == "stream"


@pytest.mark.asyncio
async def test_a_title_missing_from_the_cache_still_streams(tmp_path: Path) -> None:
    ctx = await _route(None, True, tmp_path)
    assert ctx.is_browser_game


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_not_installed_pc_only_is_a_not_installed_notice(tmp_path: Path, enabled: bool) -> None:
    _cache(tmp_path, ["xbox_pc"])
    ctx = await _route(None, enabled, tmp_path)
    assert ctx.is_xbox_pc_notice and ctx.notice_reason == "not_installed"


@pytest.mark.asyncio
async def test_no_config_or_a_broken_one_means_off(tmp_path: Path) -> None:
    _cache(tmp_path, ["xbox_pc"])
    ctx = await d._build_context(["launcher", f"microsoft:{PID}"], _Shortcuts(_ENTRY))
    assert ctx.notice_reason == "disabled"
    ctx = await _route(_ENTRY, RuntimeError("bad config"), tmp_path)
    assert ctx.notice_reason == "disabled"


def test_toggle_reads_the_real_config_manager_user_layer(tmp_path: Path) -> None:
    from unifideck.config.config_manager import ConfigManager

    repo = Path(__file__).resolve().parents[2]
    user = tmp_path / "config.json"
    user.write_text(json.dumps({"stores": {"microsoft": {"xbox_pc": {"enabled": True}}}}))
    defaults = repo / "defaults" / "config.json"
    assert routing.xbox_pc_enabled(ConfigManager(str(defaults))) is False
    assert routing.xbox_pc_enabled(ConfigManager(defaults_path=str(defaults), user_path=str(user)))


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "expected"), [
    ("auth", "auth"), ("storefront", "storefront"), ("xbox-pc-auth", "xbox-pc-auth"),
])
async def test_actions_are_not_routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str, expected: str,
) -> None:
    monkeypatch.setenv("UNIFIDECK_MICROSOFT_ACTION", action)
    ctx = await d._build_context(
        ["launcher", "microsoft:ms-auth-temp-1"], _Shortcuts(None), _Config(True),
    )
    assert ctx.is_launch_action is False
    assert ctx.action == expected and ctx.auth_store == "microsoft"


@pytest.mark.asyncio
async def test_xbox_pc_auth_is_microsoft_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIDECK_EPIC_ACTION", "xbox-pc-auth")
    assert d._detect_special_action() == (None, None, True)


@pytest.mark.asyncio
async def test_xbox_pc_auth_opens_the_microsoft_auth_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Same Edge flow as ``auth``: ``ms_auth_url.txt`` into ``launch_auth`` (CDP 9222)."""
    from unifideck.launcher.flows import auth as auth_flow
    from unifideck.services.launcher.service import LauncherService

    url_file = Path.home() / ".local/share/unifideck/ms_auth_url.txt"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text("about:blank")
    monkeypatch.setenv("UNIFIDECK_MICROSOFT_ACTION", "xbox-pc-auth")

    async def no_wait(_edge: Any) -> None:
        return None

    monkeypatch.setattr(auth_flow, "_wait_for_auth_end", no_wait)
    opened: list[tuple[str, bool]] = []
    edge = SimpleNamespace(
        is_installed=True,
        launch_auth=lambda url, *, private=False: opened.append((url, private)) or True,
    )
    svc = LauncherService(bus=SimpleNamespace(), shortcut_svc=None, proton_svc=None,  # type: ignore[arg-type]
                          cloud_svc=None, edge_browser=edge)  # type: ignore[arg-type]
    ctx = await d._build_context(["launcher", "microsoft:ms-auth-temp-1"], _Shortcuts(None))
    result = await svc._handle_auth_path(ctx)
    # InPrivate: a signed-in Edge profile sends its browser-SSO credential and
    # Microsoft then never asks for the second factor the Xbox exchange needs.
    assert result.success and opened == [("about:blank", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("store, url", [
    ("microsoft", "https://login.live.com/oauth20_authorize.srf?client_id=x"),
    ("epic", "about:blank"),
    ("gog", "https://auth.gog.com/auth?client_id=x"),
])
async def test_only_the_xbox_pc_sign_in_window_is_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: str, url: str,
) -> None:
    """Every other store keeps the shared profile (xCloud cookies, saved logins)."""
    from unifideck.launcher.flows import auth as auth_flow

    monkeypatch.setattr(auth_flow, "read_auth_url", lambda _store: url)

    async def no_wait(_edge: Any) -> None:
        return None

    monkeypatch.setattr(auth_flow, "_wait_for_auth_end", no_wait)
    opened: list[tuple[str, bool]] = []
    edge = SimpleNamespace(
        is_installed=True,
        launch_auth=lambda u, *, private=False: opened.append((u, private)) or True,
    )
    ctx = SimpleNamespace(auth_store=store, game_key="x")
    result = await auth_flow.handle_store_auth(ctx, edge)  # type: ignore[arg-type]
    assert result.success and opened == [(url, False)]
