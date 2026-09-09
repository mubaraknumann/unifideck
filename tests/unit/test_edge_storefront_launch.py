"""``auth.edge_browser.launch.launch_storefront`` — the shop window's argv.

The single most valuable assertion in this file is the profile
directory. That shared ``--user-data-dir`` is where every store's OAuth
cookies live, and reusing it is the entire mechanism by which the shop
opens already signed in. A stray ``--incognito``, ``--guest`` or a
separate profile path would leave the feature looking like it works
while silently logging the user out of everything.

The rest pins what makes this window a *shop* rather than a sign-in:
a toolbar (so Back exists), its own CDP port (so a collision can be
detected before spawning), and its own window class.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.auth.edge_browser import display as display_mod
from unifideck.auth.edge_browser import edge as edge_mod
from unifideck.auth.edge_browser import launch as launch_mod

_URL = "https://store.epicgames.com/"


class _FakeBrowser:
    """Enough of ``EdgeBrowser`` for the arg builder."""

    def __init__(self) -> None:
        self.cdp_port = 9222
        self.process = None

    def xcloud_cdp_port(self) -> int:
        return self.cdp_port + 1

    def storefront_cdp_port(self) -> int:
        return self.cdp_port + 2

    def locale_fn(self) -> str:
        return "en-US"


@pytest.fixture
def captured(monkeypatch) -> dict[str, Any]:
    """Capture the argv ``launch_storefront`` would spawn."""
    seen: dict[str, Any] = {}

    monkeypatch.setattr(launch_mod, "_prepare_for_launch", lambda b: ["edge"])
    monkeypatch.setattr(launch_mod, "clean_env", lambda: {})
    # Display detection shells out to xrandr/xdpyinfo; pin it so the test
    # neither spawns subprocesses nor depends on the host's monitor.
    monkeypatch.setattr(
        display_mod, "auth_window_flags", lambda env: ["--window-size=1280,800"],
    )

    def _fake_spawn(browser, args, log_mode, label, env=None):
        seen["args"] = args
        seen["log_mode"] = log_mode
        seen["label"] = label
        return True

    monkeypatch.setattr(launch_mod, "_spawn_edge_process", _fake_spawn)
    return seen


def _run(captured: dict[str, Any]) -> list[str]:
    assert launch_mod.launch_storefront(_FakeBrowser(), _URL) is True
    return captured["args"]


def test_it_reuses_the_shared_auth_profile(captured) -> None:
    """The session-reuse invariant. Break this and the shop is signed out."""
    args = _run(captured)
    assert f"--user-data-dir={edge_mod.PROFILE_DIR}" in args


@pytest.mark.parametrize("flag", ["--incognito", "--guest"])
def test_it_never_opens_a_sessionless_window(captured, flag: str) -> None:
    args = _run(captured)
    assert not any(a.startswith(flag) for a in args)


def test_the_url_is_positional_so_the_toolbar_survives(captured) -> None:
    """``--app=<url>`` is exactly what removes Back, and a shop needs Back."""
    args = _run(captured)
    assert args[-1] == _URL
    assert not any(a.startswith("--app=") for a in args)


def test_it_is_not_a_kiosk(captured) -> None:
    """Kiosk is the xCloud flavour; it also hides the toolbar."""
    args = _run(captured)
    assert "--kiosk" not in args
    assert "--start-maximized" in args


def test_it_takes_the_third_cdp_port(captured) -> None:
    """auth=9222, xcloud=9223, shop=9224 — the collision-detection protocol."""
    args = _run(captured)
    assert "--remote-debugging-port=9224" in args


def test_it_has_its_own_window_class(captured) -> None:
    """So the xdotool classname match for xCloud can never grab this one."""
    args = _run(captured)
    assert "--class=unifideck-store" in args


def test_it_appends_to_the_log_rather_than_truncating(captured) -> None:
    """Opening a shop must not erase the log of the sign-in before it."""
    _run(captured)
    assert captured["log_mode"] == "a"
    assert captured["label"] == "Storefront"


def test_the_port_offsets_agree_with_the_browser() -> None:
    """One source of truth for the offsets, on EdgeBrowser."""
    browser = _FakeBrowser()
    assert browser.xcloud_cdp_port() == 9223
    assert browser.storefront_cdp_port() == 9224
