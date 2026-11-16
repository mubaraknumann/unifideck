"""Regression guard for the Luna touch point in ``launcher/browser_games``.

Task 7.1 extended this *shared* module (xCloud/microsoft and itch.io also
depend on it) to accept Luna's regional stream hosts and route a
``store == "luna"`` title to a ``stream`` target. These tests pin the
pre-existing behaviour so that extension cannot silently regress the two
stores that were here first: the xCloud/microsoft ``stream`` path and the
itch.io ``web`` path must resolve to the exact same ``BrowserTarget`` (kind,
url) and keep the same URL allow-list verdicts as before Luna existed, while
Luna is confined to its own allow-listed hosts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher import browser_games
from unifideck.launcher.browser_games import BrowserTarget, browser_target


@pytest.fixture()
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "library_cache.json"
    monkeypatch.setattr(browser_games, "LIBRARY_CACHE", str(path))

    def write(libraries: dict[str, list[dict[str, Any]]]) -> None:
        path.write_text(json.dumps({"libraries": libraries}))

    return write


# ── is_allowed_browser_url: the pre-existing verdicts are unchanged ──

#: Verdicts that held before Luna was added. Each must still hold identically:
#: xCloud hosts remain the only ``stream`` hosts besides Luna, itch.io remains
#: the only ``web`` host, and nothing crosses between the two kinds.
_PRE_LUNA_VERDICTS = [
    # xCloud (microsoft) stream hosts — still accepted as stream.
    ("https://www.xbox.com/play/launch/9NBLGGH4PNC7", "stream", True),
    ("https://xbox.com/play/launch/9NBLGGH4PNC7", "stream", True),
    # itch.io web hosts — still accepted as web.
    ("https://poncle.itch.io/vampire-survivors", "web", True),
    ("https://itch.io/embed-upload/123", "web", True),
    # Cross-kind: an xCloud URL is not a web URL and vice versa.
    ("https://www.xbox.com/play/launch/9NBLGGH4PNC7", "web", False),
    ("https://poncle.itch.io/game", "stream", False),
    # Non-https and look-alikes stay refused for both kinds.
    ("http://www.xbox.com/play/launch/X", "stream", False),
    ("http://poncle.itch.io/game", "web", False),
    ("https://www.xbox.com.evil.example/play", "stream", False),
    ("https://itch.io.evil.example/login", "web", False),
    ("https://evilitch.io/game", "web", False),
    ("https://login.example/xbox.com", "stream", False),
    ("javascript:alert(1)", "web", False),
    ("file:///home/deck/.ssh/id_rsa", "web", False),
]


@pytest.mark.parametrize(("url", "kind", "allowed"), _PRE_LUNA_VERDICTS)
def test_existing_store_url_verdicts_are_unchanged(url: str, kind: str, allowed: bool) -> None:
    assert browser_games.is_allowed_browser_url(url, kind) is allowed


def test_adding_luna_did_not_widen_the_web_host_set() -> None:
    # A Luna host must never be reachable through the ``web`` kind — Luna is a
    # stream, and the web allow-list belongs to itch.io alone.
    assert browser_games.is_allowed_browser_url("https://luna.amazon.de/player", "web") is False


def test_luna_hosts_are_only_allowed_for_the_stream_kind() -> None:
    assert browser_games.is_allowed_browser_url("https://luna.amazon.de/title/abc", "stream") is True
    assert browser_games.is_allowed_browser_url("https://luna.amazon.co.uk/title/abc", "stream") is True
    # A Luna look-alike nested under another domain stays refused.
    assert browser_games.is_allowed_browser_url(
        "https://luna.amazon.de.evil.example/title/abc", "stream") is False


# ── browser_target: microsoft (xCloud) resolution is byte-for-byte unchanged ──

def test_microsoft_recorded_url_resolves_to_the_same_stream_target(cache: Any) -> None:
    cache({"microsoft": [{"store_game_id": "9NBLGGH4PNC7", "tags": ["xcloud", "browser"],
                          "metadata": {"browser_url": "https://www.xbox.com/play/launch/9NBLGGH4PNC7"}}]})
    assert browser_target("microsoft", "9NBLGGH4PNC7") == BrowserTarget(
        url="https://www.xbox.com/play/launch/9NBLGGH4PNC7", kind="stream")


def test_microsoft_fallback_url_is_unchanged(cache: Any) -> None:
    # Pre-0.7.6 cache (xcloud tag, no browser_url) still synthesises the xCloud URL.
    cache({"microsoft": [{"store_game_id": "9NBLGGH4PNC7", "tags": ["xcloud"], "metadata": {}}]})
    assert browser_target("microsoft", "9NBLGGH4PNC7") == BrowserTarget(
        url="https://www.xbox.com/play/launch/9NBLGGH4PNC7", kind="stream")


def test_microsoft_missing_from_cache_still_falls_back_to_xcloud(cache: Any) -> None:
    cache({})
    assert browser_target("microsoft", "ABC") == BrowserTarget(
        url="https://www.xbox.com/play/launch/ABC", kind="stream")


def test_microsoft_foreign_url_still_falls_back_to_xcloud(cache: Any) -> None:
    cache({"microsoft": [{"store_game_id": "ABC", "tags": ["xcloud", "browser"],
                          "metadata": {"browser_url": "https://phish.example/play"}}]})
    assert browser_target("microsoft", "ABC") == BrowserTarget(
        url="https://www.xbox.com/play/launch/ABC", kind="stream")


# ── browser_target: itch.io (web) resolution is byte-for-byte unchanged ──

def test_itch_web_target_is_unchanged(cache: Any) -> None:
    cache({"itch": [{"store_game_id": "1242468", "tags": ["browser"],
                     "metadata": {"browser_url": "https://poncle.itch.io/vampire-survivors"}}]})
    assert browser_target("itch", "1242468") == BrowserTarget(
        url="https://poncle.itch.io/vampire-survivors", kind="web")


def test_installable_itch_game_is_still_not_a_browser_game(cache: Any) -> None:
    cache({"itch": [{"store_game_id": "2119837", "tags": [], "metadata": {}}]})
    assert browser_target("itch", "2119837") is None


def test_itch_foreign_url_is_still_refused(cache: Any) -> None:
    cache({"itch": [{"store_game_id": "1", "tags": ["browser"],
                     "metadata": {"browser_url": "https://phish.example/itch-login"}}]})
    assert browser_target("itch", "1") is None


def test_non_browser_store_still_resolves_to_nothing(cache: Any) -> None:
    # A plain installable store (gog) was never a browser game; Luna must not
    # have turned the no-fallback path into a stream.
    cache({"gog": [{"store_game_id": "1", "tags": [], "metadata": {}}]})
    assert browser_target("gog", "1") is None


# ── browser_target: Luna is confined to its own allow-listed hosts ──

def test_luna_allowed_host_resolves_to_stream(cache: Any) -> None:
    cache({"luna": [{"store_game_id": "B0ABC", "tags": ["browser", "luna"],
                     "metadata": {"browser_url": "https://luna.amazon.de/title/B0ABC"}}]})
    assert browser_target("luna", "B0ABC") == BrowserTarget(
        url="https://luna.amazon.de/title/B0ABC", kind="stream")


def test_luna_disallowed_host_resolves_to_none(cache: Any) -> None:
    # Luna has no derivable fallback URL, so a disallowed host must yield None
    # (not a guessed xCloud-style URL).
    cache({"luna": [{"store_game_id": "B0ABC", "tags": ["browser", "luna"],
                     "metadata": {"browser_url": "https://phish.example/title/B0ABC"}}]})
    assert browser_target("luna", "B0ABC") is None
