"""A browser window opened from a Steam shortcut that Steam ran through Proton.

A Steam client update (beta, 2026-11-04) applied the global Steam Play tool
to Unifideck's temporary sign-in and store shortcuts, so the launcher started
inside the Steam Linux Runtime container and escaped it. Two things broke:

- the escape dropped the Steam app identity, so the sign-in window was never
  brought to the front in Gaming Mode;
- Steam's Stop no longer ended the browser (the escaped launcher is outside
  Steam's reaper), so the hidden window kept the shared profile and every
  later sign-in or store window was refused.
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.flows import browser_window

LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "unifideck-launcher"


def _load_launcher(monkeypatch: pytest.MonkeyPatch) -> Any:
    # Importing the script runs its escape check; keep it a no-op here.
    monkeypatch.delenv("container", raising=False)
    loader = importlib.machinery.SourceFileLoader("unifideck_launcher_script", str(LAUNCHER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_container_escape_carries_the_app_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    launcher = _load_launcher(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(launcher, "_in_pressure_vessel", lambda: True)
    monkeypatch.delenv(launcher._REEXEC_GUARD_ENV, raising=False)
    monkeypatch.setattr(launcher, "_find_on_path", lambda name: "/fake/launch-client")
    monkeypatch.setattr(os, "execve", lambda path, argv, env: calls.append(argv))

    launcher._escape_pressure_vessel()

    argv = calls[0]
    options = argv[: argv.index("--")]
    for name in ("SteamAppId", "SteamGameId", "SteamOverlayGameId",
                 "STEAM_COMPAT_APP_ID", launcher._REEXEC_GUARD_ENV):
        assert f"--pass-env={name}" in options
    assert "--pass-env-matching=UNIFIDECK_*" in options
    assert "--alongside-steam" in options


class _Browser:
    def __init__(self, proc: Any) -> None:
        self.process = proc
        self.killed = 0

    def kill(self) -> None:
        self.killed += 1


class _NeverExits:
    def wait(self) -> None:
        import time
        time.sleep(0.3)


def test_stop_closes_the_browser() -> None:
    browser = _Browser(_NeverExits())

    async def run() -> None:
        task = asyncio.create_task(
            browser_window.wait_for_browser_exit(browser, 60, log_tag="t"))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert browser.killed == 1


def test_the_timeout_closes_the_browser() -> None:
    browser = _Browser(_NeverExits())
    asyncio.run(browser_window.wait_for_browser_exit(browser, 0, log_tag="t"))  # type: ignore[arg-type]
    assert browser.killed == 1


class _Exits:
    def wait(self) -> int:
        return 0


def test_a_window_the_user_closed_is_left_alone() -> None:
    browser = _Browser(_Exits())
    asyncio.run(browser_window.wait_for_browser_exit(browser, 60, log_tag="t"))  # type: ignore[arg-type]
    assert browser.killed == 0


@pytest.fixture
def gaming_mode(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str | None]]:
    """A Gaming Mode session env for app 4285670493; records tagger starts."""
    import threading

    from unifideck.auth.edge_browser import env as env_mod
    from unifideck.launcher.proton.infrastructure import gamescope_window_tagger as tagger_mod

    session = {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0", "SteamGameId": "4285670493"}
    monkeypatch.setattr(env_mod, "clean_env", lambda: dict(session))
    started: list[tuple[int, str | None]] = []

    def start(appid: int, wm_class: str | None = None) -> threading.Event:
        started.append((appid, wm_class))
        return threading.Event()

    monkeypatch.setattr(tagger_mod, "start_window_tagger", start)
    return started


def test_gaming_mode_tags_the_browser_window_with_the_app(
    gaming_mode: list[tuple[int, str | None]],
) -> None:
    args = ["chrome", "--app=x", "--class=unifideck-auth", "--remote-debugging-port=9222"]
    assert browser_window.tag_browser_window(args, log_tag="t") is not None
    assert gaming_mode == [(4285670493, "unifideck-auth")]


def test_desktop_mode_tags_nothing(
    monkeypatch: pytest.MonkeyPatch, gaming_mode: list[tuple[int, str | None]],
) -> None:
    from unifideck.auth.edge_browser import env as env_mod
    monkeypatch.setattr(env_mod, "clean_env", lambda: {"SteamGameId": "4285670493"})
    assert browser_window.tag_browser_window(["chrome", "--class=unifideck-auth"],
                                             log_tag="t") is None
    assert gaming_mode == []


def test_no_app_id_tags_nothing(
    monkeypatch: pytest.MonkeyPatch, gaming_mode: list[tuple[int, str | None]],
) -> None:
    from unifideck.auth.edge_browser import env as env_mod
    monkeypatch.setattr(env_mod, "clean_env",
                        lambda: {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0", "SteamGameId": "0"})
    assert browser_window.tag_browser_window(["chrome", "--class=unifideck-auth"],
                                             log_tag="t") is None
    assert gaming_mode == []


@pytest.mark.parametrize("session", [
    # Real values from a Gaming Mode sign-in launch (2026-11-04).
    {"SteamAppId": "2625890577", "SteamGameId": "11278114151123124224"},
    {"SteamGameId": "11278114151123124224"},
    {"SteamAppId": "2625890577"},
])
def test_the_tag_is_the_32_bit_app_id(
    monkeypatch: pytest.MonkeyPatch, gaming_mode: list[tuple[int, str | None]],
    session: dict[str, str],
) -> None:
    from unifideck.auth.edge_browser import env as env_mod
    monkeypatch.setattr(env_mod, "clean_env",
                        lambda: {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0", **session})
    browser_window.tag_browser_window(["chrome", "--class=unifideck-auth"], log_tag="t")
    assert gaming_mode == [(2625890577, "unifideck-auth")]
