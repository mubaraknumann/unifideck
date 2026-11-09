"""The plugin's browser around a window: start-up download, close, environment.

Three things a device test found broken in Gaming Mode, pinned here:

- a window closed by a signal lost the cookies of a sign-in made seconds
  before, so the store window opened signed out. ``kill`` must ask the
  browser to close itself (``Browser.close``) before any signal;
- every key typed in a sign-in window arrived twice, because the browser
  loaded Steam's input-method module next to its own keyboard input.
  ``clean_env`` must hand the browser no input-method variables;
- the start-up download ran before the network was up and gave up at once.
"""
from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from unifideck.auth.edge_browser import edge as edge_mod
from unifideck.auth.edge_browser import env as env_mod
from unifideck.auth.edge_browser.chrome_installer import NETWORK_FAILURE
from unifideck.rpc.mixins.edge import EdgeRPCMixin


class _Proc:
    """A ``Popen`` stand-in that exits when told to."""

    def __init__(self) -> None:
        self.alive = True
        self.waited = False

    def poll(self) -> int | None:
        return None if self.alive else 0

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        if self.alive:
            raise subprocess.TimeoutExpired("chrome", timeout or 0)
        return 0


def _bare_browser() -> Any:
    """An ``EdgeBrowser`` without the constructor's profile side effects."""
    browser = object.__new__(edge_mod.EdgeBrowser)
    browser.process = None
    browser.process_cdp_port = None
    browser.cleanup_stale_profile_state = lambda: None
    return browser


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []
    monkeypatch.setattr(edge_mod.process_ops, "graceful_kill", sent.append)
    return sent


def test_kill_closes_the_browser_through_cdp_before_any_signal(
    monkeypatch: pytest.MonkeyPatch, signals: list[Any],
) -> None:
    browser, proc = _bare_browser(), _Proc()
    browser.process, browser.process_cdp_port = proc, 9224
    asked: list[int] = []

    def close(port: int) -> bool:
        asked.append(port)
        proc.alive = False
        return True

    monkeypatch.setattr(edge_mod, "close_browser_blocking", close)
    browser.kill()
    assert asked == [9224]
    assert signals == []
    assert browser.process is None and browser.process_cdp_port is None


def test_kill_falls_back_to_a_signal_when_the_clean_close_fails(
    monkeypatch: pytest.MonkeyPatch, signals: list[Any],
) -> None:
    browser, proc = _bare_browser(), _Proc()
    browser.process, browser.process_cdp_port = proc, 9222
    monkeypatch.setattr(edge_mod, "close_browser_blocking", lambda port: False)
    browser.kill()
    assert signals == [proc]


def test_kill_without_a_known_port_signals_directly(
    monkeypatch: pytest.MonkeyPatch, signals: list[Any],
) -> None:
    browser, proc = _bare_browser(), _Proc()
    browser.process = proc
    monkeypatch.setattr(edge_mod, "close_browser_blocking",
                        lambda port: pytest.fail("no port to close on"))
    browser.kill()
    assert signals == [proc]


def test_the_browser_gets_no_input_method_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What Gaming Mode exports into the plugin's environment.
    monkeypatch.setenv("GTK_IM_MODULE", "Steam")
    monkeypatch.setenv("QT_IM_MODULE", "steam")
    monkeypatch.setenv("XMODIFIERS", "@im=Steam")
    env = env_mod.clean_env()
    assert not {"GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS"} & set(env)


def _installer_returning(results: list[dict[str, Any]], calls: list[int]) -> Any:
    async def install() -> dict[str, Any]:
        calls.append(1)
        return results[min(len(calls), len(results)) - 1]
    return install


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(edge_mod.asyncio, "sleep", sleep)
    return waits


def _run_background(browser: Any) -> None:
    asyncio.run(browser._background_install())


def test_startup_download_retries_until_the_network_answers(
    monkeypatch: pytest.MonkeyPatch, no_wait: list[float],
) -> None:
    browser, calls = _bare_browser(), []
    offline = {"success": False, "error": NETWORK_FAILURE}
    browser.install = _installer_returning([offline, offline, {"success": True}], calls)
    monkeypatch.setattr(edge_mod.EdgeBrowser, "is_installed", property(lambda self: False))
    _run_background(browser)
    assert len(calls) == 3
    assert no_wait == list(edge_mod._BACKGROUND_INSTALL_RETRY_S[:2])


def test_startup_download_does_not_retry_a_bad_build(
    monkeypatch: pytest.MonkeyPatch, no_wait: list[float],
) -> None:
    browser, calls = _bare_browser(), []
    bad = {"success": False, "error": "microsoft.browserInstallFailed"}
    browser.install = _installer_returning([bad], calls)
    monkeypatch.setattr(edge_mod.EdgeBrowser, "is_installed", property(lambda self: False))
    _run_background(browser)
    assert calls == [1]
    assert no_wait == []


def test_startup_download_gives_up_after_the_last_retry(
    monkeypatch: pytest.MonkeyPatch, no_wait: list[float],
) -> None:
    browser, calls = _bare_browser(), []
    browser.install = _installer_returning([{"success": False, "error": NETWORK_FAILURE}], calls)
    monkeypatch.setattr(edge_mod.EdgeBrowser, "is_installed", property(lambda self: False))
    _run_background(browser)
    assert len(calls) == len(edge_mod._BACKGROUND_INSTALL_RETRY_S) + 1


class _Registry:
    def __init__(self, store: Any) -> None:
        self._store = store

    def get(self, name: str) -> Any:
        return self._store if name == "microsoft" else None


class _Store:
    def __init__(self, installed: bool) -> None:
        self._installed = installed

    def is_edge_installed(self) -> bool:
        return self._installed


@pytest.mark.parametrize(("store", "expected"), [
    (_Store(True), True),
    (_Store(False), False),
    (None, False),
])
def test_browser_installed_rpc(store: Any, expected: bool) -> None:
    mixin = EdgeRPCMixin()
    mixin.registry = _Registry(store)
    assert asyncio.run(mixin.browser_installed()) == {"installed": expected}
