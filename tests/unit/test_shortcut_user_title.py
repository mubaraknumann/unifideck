"""Guard test — a shortcut the user renamed in Steam keeps its name.

Reconcile used to write ``game.title`` over ``AppName`` on every sync: the
registry-reclaim path runs for every registered game, so even a plain sync
rewrote it. A user who renamed "Alan Wake 2 AnkerGames" to "Alan Wake 2" got
the old name back on the next sync.

What is pinned:

1. a rename survives plain syncs, Force Sync, and a later store-side title
   change;
2. an untouched name still follows the store, so a store-side fix still
   reaches Steam;
3. with no recorded title the store wins, so a stale name is never frozen;
4. the registry keeps recording the store title, not the user's name.
"""

from __future__ import annotations

from typing import Any

import pytest

from unifideck.core.types import Game
from unifideck.event_bus.event_bus import EventBus
from unifideck.services.shortcut.service import ShortcutService
from unifideck.services.shortcut.user_title import (
    get_registered_title,
    resolve_app_name,
)

_LAUNCHER = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"
_KEY = "gamevault:lv_1"


def _service(tmp_path: Any) -> ShortcutService:
    return ShortcutService(
        EventBus(),
        str(tmp_path / "shortcuts.vdf"),
        str(tmp_path / "games.map"),
        launcher_path=_LAUNCHER,
    )


def _game(title: str) -> Game:
    return Game(
        app_id=0, store="gamevault", store_game_id="lv_1",
        title=title, installed=False,
    )


def _sync(
    svc: ShortcutService,
    shortcuts: dict[str, Any],
    registry: dict[str, Any],
    title: str,
    *,
    force: bool = False,
) -> str:
    launch_to_key = {
        e["LaunchOptions"]: k for k, e in shortcuts.items()
    }
    return svc._sync_one_game(
        _game(title), shortcuts, registry, launch_to_key,
        launcher=_LAUNCHER, force=force,
    )


def _only(shortcuts: dict[str, Any]) -> dict[str, Any]:
    assert len(shortcuts) == 1
    return next(iter(shortcuts.values()))


# ── the rule ───────────────────────────────────────────────────────

@pytest.mark.parametrize(("current", "last", "store", "expected"), [
    ("Alan Wake 2", "Alan Wake 2 AnkerGames", "Alan Wake 2 AnkerGames",
     "Alan Wake 2"),
    ("Old", "Old", "New", "New"),
    ("Anything", None, "Store", "Store"),
    ("", "Old", "Store", "Store"),
    ("   ", "Old", "Store", "Store"),
    (None, "Old", "Store", "Store"),
    (42, "Old", "Store", "Store"),
])
def test_resolve_app_name(current, last, store, expected) -> None:
    assert resolve_app_name(current, last, store) == expected


def test_get_registered_title_ignores_malformed_rows() -> None:
    assert get_registered_title({}, _KEY) is None
    assert get_registered_title({_KEY: "junk"}, _KEY) is None
    assert get_registered_title({_KEY: {"title": ""}}, _KEY) is None
    assert get_registered_title({_KEY: {"title": 7}}, _KEY) is None
    assert get_registered_title({_KEY: {"title": "T"}}, _KEY) == "T"


# ── through reconcile ──────────────────────────────────────────────

def test_rename_survives_plain_and_force_syncs(tmp_path) -> None:
    svc = _service(tmp_path)
    shortcuts: dict[str, Any] = {}
    registry: dict[str, Any] = {}

    assert _sync(svc, shortcuts, registry, "Alan Wake 2 AnkerGames") == "added"
    _only(shortcuts)["AppName"] = "Alan Wake 2"  # the user, in Steam

    assert _sync(svc, shortcuts, registry, "Alan Wake 2 AnkerGames") == "reclaimed"
    assert _only(shortcuts)["AppName"] == "Alan Wake 2"

    _sync(svc, shortcuts, registry, "Alan Wake 2 AnkerGames", force=True)
    assert _only(shortcuts)["AppName"] == "Alan Wake 2"

    _sync(svc, shortcuts, registry, "Alan Wake II")
    assert _only(shortcuts)["AppName"] == "Alan Wake 2"
    assert registry[_KEY]["title"] == "Alan Wake II", (
        "the registry must record the store title, or the next sync would "
        "read the user's name as ours and overwrite it"
    )


def test_untouched_name_follows_the_store(tmp_path) -> None:
    svc = _service(tmp_path)
    shortcuts: dict[str, Any] = {}
    registry: dict[str, Any] = {}

    _sync(svc, shortcuts, registry, "Alan Wake 2 AnkerGames")
    _sync(svc, shortcuts, registry, "Alan Wake 2")

    assert _only(shortcuts)["AppName"] == "Alan Wake 2"


def test_renaming_back_to_the_store_title_hands_control_back(tmp_path) -> None:
    svc = _service(tmp_path)
    shortcuts: dict[str, Any] = {}
    registry: dict[str, Any] = {}

    _sync(svc, shortcuts, registry, "Game")
    _only(shortcuts)["AppName"] = "My Name"
    _sync(svc, shortcuts, registry, "Game")
    _only(shortcuts)["AppName"] = "Game"
    _sync(svc, shortcuts, registry, "Game")
    _sync(svc, shortcuts, registry, "Game: Remastered")

    assert _only(shortcuts)["AppName"] == "Game: Remastered"


def test_no_recorded_title_lets_the_store_win(tmp_path) -> None:
    """A lost registry must not freeze whatever name is in the vdf."""
    svc = _service(tmp_path)
    shortcuts: dict[str, Any] = {}
    _sync(svc, shortcuts, {}, "Game")
    _only(shortcuts)["AppName"] = "C:\\Users\\me\\Vault\\files\\Game"

    _sync(svc, shortcuts, {}, "Game", force=True)

    assert _only(shortcuts)["AppName"] == "Game"


def test_appid_match_without_force_records_the_name_left_in_place(
    tmp_path,
) -> None:
    """The appid fallback does not rewrite ``AppName`` on a plain sync.

    Recording ``game.title`` there would make the untouched old name look
    like a rename on the next sync and freeze it.
    """
    svc = _service(tmp_path)
    shortcuts: dict[str, Any] = {}
    _sync(svc, shortcuts, {}, "Old Name")
    entry = _only(shortcuts)
    entry["LaunchOptions"] = ""  # Steam mangled it: only the appid matches
    registry: dict[str, Any] = {}

    assert _sync(svc, shortcuts, registry, "New Name") == "kept"
    assert registry[_KEY]["title"] == "Old Name"

    _sync(svc, shortcuts, registry, "New Name")
    assert entry["AppName"] == "New Name"
