"""The achievement watcher: which stores it follows, toasts, last session."""
from __future__ import annotations

import asyncio
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
def make(tmp_path, monkeypatch):
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
        for key in list(watcher._poll_tasks):
            watcher._cancel(key)


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
