"""The achievement watcher: which stores it follows, toasts, last session."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from unifideck.services.achievements import watcher as w
from unifideck.services.achievements.state import AchievementStateStore
from unifideck.stores.shared.achievements_error import StoreAchievementsError
from unifideck.stores.shared.achievements_payload import build_payload


def _payload(store: str, unlocked: list[str], locked: list[str]) -> dict[str, Any]:
    items = [
        {"key": k, "name": k.upper(), "unlocked": True, "unlocked_at": float(i + 1)}
        for i, k in enumerate(unlocked)
    ] + [{"key": k, "name": k.upper(), "unlocked": False, "unlocked_at": None} for k in locked]
    return build_payload(store, "g1", items)


class _Store:
    def __init__(self, store: str, payloads: list[Any]) -> None:
        self.store = store
        self._payloads = payloads
        self.invalidated: list[str] = []
        self.calls = 0

    async def get_game_achievements(self, game_id: str, force: bool = False) -> dict[str, Any]:
        self.calls += 1
        item = self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        if isinstance(item, Exception):
            raise item
        return item

    def invalidate_achievements(self, game_id: str) -> None:
        self.invalidated.append(game_id)


@pytest.fixture
async def make(tmp_path, monkeypatch):
    """A watcher on a fake registry, state in tmp_path, toasts captured."""
    monkeypatch.setattr(w, "_RECONCILE_RETRY_DELAY_SECONDS", 0.0)
    made: list[w.AchievementWatcher] = []

    def build(stores: dict[str, _Store]) -> tuple[w.AchievementWatcher, list[str]]:
        registry = MagicMock()
        registry.get_store.side_effect = stores.get
        watcher = w.AchievementWatcher(MagicMock(), registry)
        watcher._state = AchievementStateStore(tmp_path / "state.json")
        toasts: list[str] = []
        monkeypatch.setattr(watcher, "_toast", lambda store, gid, ach: toasts.append(f"{store}:{ach['key']}"))
        made.append(watcher)
        return watcher, toasts

    yield build
    for watcher in made:
        tasks = list(watcher._poll_tasks.values())
        await watcher.stop()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _drain() -> None:
    while w._BACKGROUND:
        await asyncio.gather(*list(w._BACKGROUND))


@pytest.mark.parametrize("store", ["gog", "epic", "microsoft"])
async def test_session_toasts_new_unlock_once_and_records_summary(make, store: str) -> None:
    before = _payload(store, ["a"], ["b", "c"])
    after = _payload(store, ["a", "b"], ["c"])
    inst = _Store(store, [before, after])
    watcher, toasts = make({store: inst})

    await watcher._on_game_launched(store=store, game_id="g1")
    await watcher._on_game_stopped(store=store, game_id="g1")
    await _drain()

    assert toasts == [f"{store}:b"]
    assert inst.invalidated == ["g1"]
    last = await watcher.get_last_session(store, "g1")
    assert last is not None
    assert last["names"] == ["B"]
    assert last["unlocked"] == 1
    assert last["total"] == 3


async def test_other_stores_are_ignored(make) -> None:
    inst = _Store("amazon", [_payload("amazon", [], ["a"])])
    watcher, _ = make({"amazon": inst})
    await watcher._on_game_launched(store="amazon", game_id="g1")
    await watcher._on_game_stopped(store="amazon", game_id="g1")
    await _drain()
    assert inst.calls == 0
    assert watcher._poll_tasks == {}


async def test_store_error_falls_back_to_saved_baseline(make) -> None:
    after = _payload("microsoft", ["a", "b"], [])
    inst = _Store("microsoft", [StoreAchievementsError("offline"), after])
    watcher, toasts = make({"microsoft": inst})
    await asyncio.to_thread(watcher._state.update, "microsoft", "g1", unlocked_keys={"a"})

    await watcher._on_game_launched(store="microsoft", game_id="g1")
    await watcher._on_game_stopped(store="microsoft", game_id="g1")
    await _drain()
    assert toasts == ["microsoft:b"]


def test_microsoft_polls_more_gently() -> None:
    assert w._POLL_INTERVALS["microsoft"] > w._POLL_INTERVAL_SECONDS


def test_toast_carries_the_achievement_card(monkeypatch) -> None:
    from unifideck.launcher import frontend_bridge

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(frontend_bridge, "launcher_toast", lambda key, **kw: sent.append(kw))
    watcher = w.AchievementWatcher(MagicMock(), None)
    watcher._toast("microsoft", "9NNM7PKZN3JF", {
        "key": "k", "name": "Like a Boss", "image_unlocked": "https://images-eds-ssl.xboxlive.com/i",
        "rarity": 4.2,
    })
    assert sent[0]["achievement"] == {
        "name": "Like a Boss",
        "icon": "https://images-eds-ssl.xboxlive.com/i",
        "store": "microsoft",
        "rarity": 4.2,
    }
    assert sent[0]["i18n_params"] == {"name": "Like a Boss"}  # text fallback kept


def _toasted_title(monkeypatch, tmp_path, registry: dict[str, Any]) -> str:
    """The ``game_title`` a toast carries with *registry* as the shortcut registry."""
    from unifideck.launcher import frontend_bridge, game_title

    path = tmp_path / "shortcuts_registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(game_title, "REGISTRY_PATH", path)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(frontend_bridge, "launcher_toast", lambda key, **kw: sent.append(kw))
    w.AchievementWatcher(MagicMock(), None)._toast("microsoft", "ABC123", {"key": "k", "name": "n"})
    return str(sent[0]["game_title"])


def test_toast_names_the_game_from_the_shortcut_registry(monkeypatch, tmp_path) -> None:
    title = _toasted_title(monkeypatch, tmp_path, {"microsoft:ABC123": {"title": "Some Game"}})
    assert title == "Some Game"


def test_toast_never_shows_the_game_id(monkeypatch, tmp_path) -> None:
    # Empty, so the frontend heads the card with the store name instead.
    assert _toasted_title(monkeypatch, tmp_path, {}) == ""


# -- comet push (GOG) ------------------------------------------------------

def _comet_item(key: str) -> dict[str, Any]:
    """A raw GOG achievement item, as comet posts it in ``data``."""
    return {
        "achievement_id": "1", "achievement_key": key, "name": key.upper(),
        "description": "", "image_url_locked": "", "image_url_unlocked": "u.png",
        "visible": True, "date_unlocked": "2026-11-04T10:00:00+00:00", "rarity": 5.0,
    }


async def test_comet_push_toasts_once_and_poll_does_not_repeat(make) -> None:
    before = _payload("gog", ["a"], ["b"])
    inst = _Store("gog", [before])
    watcher, toasts = make({"gog": inst})
    await watcher._on_game_launched(store="gog", game_id="g1")

    await watcher._on_comet_unlock("cid", _comet_item("b"))
    assert toasts == ["gog:b"]

    # The poll / reconcile later sees the same unlock: no second toast.
    watcher._emit_new_unlocks("gog", "g1", _payload("gog", ["a", "b"], []))
    await watcher._on_comet_unlock("cid", _comet_item("b"))
    assert toasts == ["gog:b"]


async def test_comet_push_without_running_gog_game_is_dropped(make) -> None:
    watcher, toasts = make({})
    await watcher._on_comet_unlock("cid", _comet_item("b"))
    assert toasts == []


async def test_comet_push_routes_by_client_id_with_two_gog_games(make, monkeypatch) -> None:
    inst = _Store("gog", [_payload("gog", [], ["b"])])
    watcher, toasts = make({"gog": inst})
    await watcher._on_game_launched(store="gog", game_id="g1")
    await watcher._on_game_launched(store="gog", game_id="g2")
    monkeypatch.setattr(w, "fetch_gog_client_creds", lambda gid: ({"g1": "c1", "g2": "c2"}[gid], "s"))

    toasted: list[str] = []
    monkeypatch.setattr(watcher, "_toast", lambda store, gid, ach: toasted.append(f"{gid}:{ach['key']}"))
    await watcher._on_comet_unlock("c2", _comet_item("b"))
    await watcher._on_comet_unlock("c9", _comet_item("b"))
    assert toasted == ["g2:b"]
    assert toasts == []


async def test_comet_receiver_start_failure_keeps_watcher_alive(make, monkeypatch, caplog) -> None:
    class _Broken:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        async def start(self) -> None:
            raise OSError("no socket")

    monkeypatch.setattr(w, "CometEventReceiver", _Broken)
    watcher, _ = make({})
    await watcher.start()
    assert watcher._comet is None
    assert "fall back" in caplog.text


async def test_comet_receiver_started_and_stopped_with_watcher(make, tmp_path) -> None:
    watcher, _ = make({})
    await watcher.start()
    try:
        assert watcher._comet is not None
        config = watcher._comet._config_file
        assert config.is_file()
    finally:
        await watcher.stop()
    assert watcher._comet is None
    assert not config.exists()
