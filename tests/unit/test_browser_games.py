"""Tests for ``launcher/browser_games``: which games open in an Edge window.

Browser games are decided per game from the library cache, so a store that
mixes installable and browser-only games (itch.io) works, and xCloud keeps
working for a cache written before ``browser_url`` existed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher import browser_games
from unifideck.launcher.browser_games import BrowserTarget, browser_target, cached_game


@pytest.fixture()
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "library_cache.json"
    monkeypatch.setattr(browser_games, "LIBRARY_CACHE", str(path))

    def write(libraries: dict[str, list[dict[str, Any]]]) -> None:
        path.write_text(json.dumps({"libraries": libraries}))

    return write


def test_itch_web_game_opens_its_page(cache: Any) -> None:
    cache({"itch": [{"store_game_id": "1242468", "tags": ["browser"],
                     "metadata": {"browser_url": "https://poncle.itch.io/vampire-survivors"}}]})
    assert browser_target("itch", "1242468") == BrowserTarget(
        url="https://poncle.itch.io/vampire-survivors", kind="web")


def test_installable_itch_game_is_not_a_browser_game(cache: Any) -> None:
    cache({"itch": [{"store_game_id": "2119837", "tags": [], "metadata": {}}]})
    assert browser_target("itch", "2119837") is None


def test_a_browser_url_without_the_tag_is_ignored(cache: Any) -> None:
    cache({"itch": [{"store_game_id": "1", "tags": [], "metadata": {"browser_url": "https://x"}}]})
    assert browser_target("itch", "1") is None


def test_xcloud_title_uses_its_recorded_url_as_a_stream(cache: Any) -> None:
    cache({"microsoft": [{"store_game_id": "9NBLGGH4PNC7", "tags": ["xcloud", "browser"],
                          "metadata": {"browser_url": "https://www.xbox.com/play/launch/9NBLGGH4PNC7"}}]})
    assert browser_target("microsoft", "9NBLGGH4PNC7") == BrowserTarget(
        url="https://www.xbox.com/play/launch/9NBLGGH4PNC7", kind="stream")


def test_pre_0_7_6_xcloud_cache_still_launches(cache: Any) -> None:
    cache({"microsoft": [{"store_game_id": "9NBLGGH4PNC7", "tags": ["xcloud"], "metadata": {}}]})
    target = browser_target("microsoft", "9NBLGGH4PNC7")
    assert target == BrowserTarget(url="https://www.xbox.com/play/launch/9NBLGGH4PNC7", kind="stream")


def test_microsoft_title_missing_from_the_cache_is_still_a_stream(cache: Any) -> None:
    cache({})
    target = browser_target("microsoft", "ABC")
    assert target is not None and target.kind == "stream" and target.url.endswith("/ABC")


def test_unreadable_cache_means_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "library_cache.json"
    bad.write_text("{not json")
    monkeypatch.setattr(browser_games, "LIBRARY_CACHE", str(bad))
    assert cached_game("itch", "1") is None
    assert browser_target("gog", "1") is None


def test_microsoft_catalog_marks_xcloud_titles_as_browser_games() -> None:
    from unifideck.stores.microsoft.microsoft_catalog import MicrosoftCatalogReader

    games = MicrosoftCatalogReader._build_xcloud_games(
        [{"details": {"productId": "9NBLGGH4PNC7"}, "titleId": "1"}], {},
    )
    assert games[0].tags == ["xcloud", "browser"]
    assert games[0].metadata["browser_url"] == "https://www.xbox.com/play/launch/9NBLGGH4PNC7"
