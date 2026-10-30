"""The Xbox PC sign-in window is opened InPrivate; every other auth window is not.

A signed-in Edge profile attaches its browser-SSO credential to every
``login.live.com`` request. Microsoft then treats the later legs of the Xbox
sign-in as single sign-on and refuses the token exchange (0x800488e5) for
ever. An InPrivate window has no browser account.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.auth.edge_browser import display as display_mod
from unifideck.auth.edge_browser import launch as launch_mod


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    spawned: list[list[str]] = []
    monkeypatch.setattr(launch_mod, "_prepare_for_launch", lambda _b: ["edge"])
    monkeypatch.setattr(launch_mod, "clean_env", lambda: {})
    monkeypatch.setattr(display_mod, "auth_window_flags", lambda _env: ["--window-size=1280,800"])

    def spawn(_browser: Any, args: list[str], **_kw: Any) -> bool:
        spawned.append(args)
        return True

    monkeypatch.setattr(launch_mod, "_spawn_edge_process", spawn)
    return spawned


def _browser() -> Any:
    return SimpleNamespace(cdp_port=9222, locale_fn=lambda: "en-US")


def test_private_auth_window_is_inprivate_and_keeps_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = _capture(monkeypatch)
    assert launch_mod.launch_auth(_browser(), "about:blank", private=True)
    args = spawned[0]
    assert "--inprivate" in args
    assert "--app=about:blank" in args and "--remote-debugging-port=9222" in args
    assert any(a.startswith("--user-data-dir=") for a in args)


def test_default_auth_window_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = _capture(monkeypatch)
    assert launch_mod.launch_auth(_browser(), "https://example.test/auth")
    assert "--inprivate" not in spawned[0]
