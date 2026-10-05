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


# ── Only allowed origins open, and web games never see the sign-in profile ──
@pytest.mark.parametrize(("url", "kind", "allowed"), [
    ("https://poncle.itch.io/vampire-survivors", "web", True),
    ("https://itch.io/embed-upload/123", "web", True),
    ("http://poncle.itch.io/game", "web", False),            # not https
    ("javascript:alert(1)", "web", False),
    ("file:///home/deck/.ssh/id_rsa", "web", False),
    ("https://itch.io.evil.example/login", "web", False),     # look-alike host
    ("https://evilitch.io/game", "web", False),
    ("https://www.xbox.com/play/launch/9NBLGGH4PNC7", "web", False),
    ("https://www.xbox.com/play/launch/9NBLGGH4PNC7", "stream", True),
    ("https://poncle.itch.io/game", "stream", False),
    ("https://login.example/xbox.com", "stream", False),
])
def test_is_allowed_browser_url(url: str, kind: str, allowed: bool) -> None:
    assert browser_games.is_allowed_browser_url(url, kind) is allowed


def test_a_web_game_with_a_foreign_url_is_not_launched(cache: Any) -> None:
    cache({"itch": [{"store_game_id": "1", "tags": ["browser"],
                     "metadata": {"browser_url": "https://phish.example/itch-login"}}]})
    assert browser_target("itch", "1") is None


def test_a_stream_with_a_foreign_url_falls_back_to_xbox(cache: Any) -> None:
    cache({"microsoft": [{"store_game_id": "ABC", "tags": ["xcloud", "browser"],
                          "metadata": {"browser_url": "https://phish.example/play"}}]})
    assert browser_target("microsoft", "ABC") == BrowserTarget(
        url="https://www.xbox.com/play/launch/ABC", kind="stream")


def _spawned_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str) -> list[str]:
    from unifideck.auth.edge_browser import edge, launch

    monkeypatch.setattr(edge, "WEB_GAME_PROFILE_DIR", str(tmp_path / "edge-webgames"))
    monkeypatch.setattr(launch, "_prepare_for_launch", lambda _b: ["edge"])
    captured: list[list[str]] = []
    monkeypatch.setattr(
        launch, "_spawn_edge_process",
        lambda _b, args, **_k: captured.append(args) or True,
    )

    class _Browser:
        def browser_game_cdp_port(self) -> int:
            return 9223

        def locale_fn(self) -> str:
            return "en-US"

    assert launch.launch_browser_game(_Browser(), "https://x", kind=kind)  # type: ignore[arg-type]
    return captured[0]


def test_web_game_runs_in_its_own_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unifideck.auth.edge_browser.edge import PROFILE_DIR

    args = _spawned_args(monkeypatch, tmp_path, "web")
    assert f"--user-data-dir={tmp_path / 'edge-webgames'}" in args
    assert f"--user-data-dir={PROFILE_DIR}" not in args
    assert (tmp_path / "edge-webgames").is_dir()


def test_xcloud_stream_keeps_the_sign_in_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unifideck.auth.edge_browser.edge import PROFILE_DIR

    args = _spawned_args(monkeypatch, tmp_path, "stream")
    assert f"--user-data-dir={PROFILE_DIR}" in args


async def test_run_browser_game_refuses_a_foreign_url() -> None:
    from types import SimpleNamespace

    from unifideck.services.launcher.browser_game import run_browser_game

    launched: list[str] = []
    events: list[Any] = []

    async def emit(*a: Any, **k: Any) -> None:
        events.append((a, k))

    svc = SimpleNamespace(
        _edge_browser=SimpleNamespace(
            is_installed=True,
            launch_browser_game=lambda url, **_k: launched.append(url) or True,
        ),
        _bus=SimpleNamespace(emit=emit),
    )
    ctx = SimpleNamespace(browser_url="https://phish.example/", browser_kind="web",
                          store="itch", game_id="1", game_key="itch:1")

    result = await run_browser_game(svc, ctx)  # type: ignore[arg-type]

    assert result.success is False and result.error == "browser_url_refused"
    assert launched == [] and events == []
