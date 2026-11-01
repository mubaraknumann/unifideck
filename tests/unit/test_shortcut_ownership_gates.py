"""The user's own non-Steam shortcuts survive every sync-path mutation.

Two of the paths covered here had **no tests at all** while being the
hottest mutation sites in the subsystem:

* ``dedup.find_duplicate_losers`` — grouped every entry in the file by
  launch-options and deleted all but the richest, with no ownership
  check. Two of the user's shortcuts sharing a launch-options string is
  ordinary (several ROMs behind one emulator launcher, two tiles for one
  game with different Proton args), and one of them was deleted.
* ``_try_reclaim_orphan`` → ``_find_existing_shortcut_key`` — matched on
  ``appid`` alone, then rewrote the matched entry's AppName, Exe,
  LaunchOptions, icon *and tags*. Non-Steam appids are a 2**31 space
  shared with Steam, NonSteamLaunchers and Heroic, so a collision
  converts one of the user's games into one of ours. Reconcile tallies
  that as ``reclaimed``, never ``removed`` — which is precisely why it
  could happen without leaving a trace in the logs.

This is the UD-006 failure class. The Exe gate is the fix in both cases;
these tests are what keeps it there.
"""
from __future__ import annotations

import asyncio
from typing import Any

from unifideck.core.types import Game
from unifideck.event_bus.event_bus import EventBus
from unifideck.services.shortcut.dedup import find_duplicate_losers
from unifideck.services.shortcut.games_map import UNIFIDECK_TAG
from unifideck.services.shortcut.reconcile_helpers import dedup_shortcuts
from unifideck.services.shortcut.service import ShortcutService

_LAUNCHER = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"


def _ours(appid: int, launch: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "appid": appid,
        "AppName": f"Our Game {appid}",
        "Exe": f'"{_LAUNCHER}"',
        "LaunchOptions": launch,
        "tags": {"0": UNIFIDECK_TAG},
    }
    entry.update(extra)
    return entry


def _rom(appid: int, launch: str, **extra: Any) -> dict[str, Any]:
    """A Steam ROM Manager / EmuDeck-style entry: emulator Exe, ROM args."""
    entry: dict[str, Any] = {
        "appid": appid,
        "AppName": f"ROM {appid}",
        "Exe": '"/run/media/Games_SSD/Emulation/tools/launchers/es-de/es-de.sh"',
        "LaunchOptions": launch,
        "StartDir": '"/run/media/Games_SSD/Emulation"',
        "tags": {"0": "Nintendo 64"},
    }
    entry.update(extra)
    return entry


# ── dedup ──────────────────────────────────────────────────────────

def test_foreign_shortcuts_sharing_launch_options_all_survive() -> None:
    """Several ROMs behind one launcher are not duplicates of each other."""
    shortcuts = {
        "0": _rom(-100, "-f %ROM%"),
        "1": _rom(-200, "-f %ROM%"),
        "2": _rom(-300, "-f %ROM%"),
    }

    losers = find_duplicate_losers(shortcuts, _LAUNCHER)

    assert losers == []


def test_dedup_still_collapses_our_own_duplicates() -> None:
    """The feature still works — Steam does produce real dupes of ours."""
    shortcuts = {
        "0": _ours(-100, "epic:1", LastPlayTime=1700000000, icon="/g/1.png"),
        "1": _ours(-101, "epic:1"),
    }

    losers = find_duplicate_losers(shortcuts, _LAUNCHER)

    # The richer entry (playtime + icon) wins; the bare clone loses.
    assert losers == ["1"]


def test_dedup_never_picks_a_foreign_entry_over_ours() -> None:
    """A foreign entry carrying our token must not enter the contest.

    Before the gate the two were scored against each other, so a
    metadata-rich foreign shortcut could win and *our* entry be deleted
    — or, worse, lose and be deleted itself.
    """
    shortcuts = {
        "0": _ours(-100, "epic:1"),
        "1": _rom(-200, "epic:1", LastPlayTime=1700000000, icon="/g/x.png"),
    }

    dropped = dedup_shortcuts(shortcuts, _LAUNCHER)

    assert dropped == 0
    assert set(shortcuts) == {"0", "1"}


def test_dedup_bracket_normalisation_does_not_group_foreign_roms() -> None:
    """ROM filenames end in ``[!]``-style tags; that strip is ours alone.

    ``_normalize_launch_options`` removes a trailing ``[...]`` so our own
    ``epic:1 [extra=1]`` matches ``epic:1``. Applied to No-Intro ROM
    paths it collapses distinct games into one group.
    """
    shortcuts = {
        "0": _rom(-100, "/roms/n64/Mario 64 (USA) [!]"),
        "1": _rom(-200, "/roms/n64/Zelda OoT (USA) [!]"),
        "2": _rom(-300, "/roms/n64/Mario 64 (USA) [b1]"),
    }

    assert find_duplicate_losers(shortcuts, _LAUNCHER) == []


# ── reclaim-by-appid ───────────────────────────────────────────────

def _service(tmp_path: Any) -> ShortcutService:
    return ShortcutService(
        EventBus(),
        str(tmp_path / "shortcuts.vdf"),
        str(tmp_path / "games.map"),
        launcher_path=_LAUNCHER,
    )


def _game(store: str, game_id: str, app_id: int) -> Game:
    return Game(
        app_id=app_id, store=store, store_game_id=game_id,
        title="Reclaimed Title", installed=False,
    )


def test_foreign_entry_with_colliding_appid_is_not_hijacked(tmp_path) -> None:
    """An appid collision must not convert the user's game into ours."""
    svc = _service(tmp_path)
    collided = -424242
    shortcuts = {"0": _rom(collided, "/roms/snes/Chrono Trigger.sfc")}
    registry = {"epic:abc": {"appid": collided, "title": "Some Epic Game"}}

    reclaimed = svc._try_reclaim_orphan(
        shortcuts, registry, _game("epic", "abc", collided), "epic:abc",
    )

    assert reclaimed is False
    assert shortcuts["0"]["AppName"] == f"ROM {collided}"
    assert "es-de.sh" in shortcuts["0"]["Exe"]
    assert shortcuts["0"]["LaunchOptions"] == "/roms/snes/Chrono Trigger.sfc"
    assert shortcuts["0"]["tags"] == {"0": "Nintendo 64"}


def test_our_own_orphan_is_still_reclaimed(tmp_path) -> None:
    """The feature still works for entries that are actually ours."""
    svc = _service(tmp_path)
    appid = -515151
    # Ours, but Steam mangled the LaunchOptions away. The name is the one we
    # last wrote, so it follows the store (a different one is a user rename,
    # pinned in test_shortcut_user_title.py).
    shortcuts = {"0": _ours(appid, "", AppName="Old Title")}
    registry = {"epic:abc": {"appid": appid, "title": "Old Title"}}

    reclaimed = svc._try_reclaim_orphan(
        shortcuts, registry, _game("epic", "abc", appid), "epic:abc",
    )

    assert reclaimed is True
    assert shortcuts["0"]["AppName"] == "Reclaimed Title"
    assert shortcuts["0"]["LaunchOptions"] == "epic:abc"
    assert shortcuts["0"]["appid"] == appid, "appid must survive — artwork keys off it"


# ── our own shortcuts still get written ────────────────────────────

def test_a_foreign_entry_with_our_token_does_not_swallow_our_game(
    tmp_path,
) -> None:
    """Our game must still get a tile even if a foreign row claims its id.

    NonSteamLaunchers writes ``battlenet:<id>`` into its own Battle.net
    row. When the launch-options index was ungated, that row *claimed*
    the game: reconcile updated the user's shortcut instead of creating
    ours, the stale-sweep dropped the row it had just rewritten, and the
    merge restored the user's original from disk. Their shortcut
    survived and **ours was never written** — the game silently absent
    from the library, counted as ``kept`` in the tally.
    """
    svc = _service(tmp_path)
    path = tmp_path / "shortcuts.vdf"
    nsl = {
        "appid": -333, "AppName": "NSL Battle.net",
        "Exe": '"/home/deck/NonSteamLaunchers/Battle.net.sh"',
        "LaunchOptions": "battlenet:s1",
    }
    import vdf
    path.write_bytes(vdf.binary_dumps({"shortcuts": {"0": nsl}}))

    asyncio.run(svc.reconcile(
        [_game("battlenet", "s1", -999)],
        force=True, valid_stores={"battlenet"},
    ))

    from unifideck.services.shortcut.vdf_read import read_vdf_sync
    entries = read_vdf_sync(str(path)).data["shortcuts"].values()
    ours = [e for e in entries if _LAUNCHER in (e.get("Exe") or "")]
    theirs = [e for e in entries if _LAUNCHER not in (e.get("Exe") or "")]

    assert len(ours) == 1, "our game must have its own shortcut"
    assert ours[0]["AppName"] == "Reclaimed Title"
    assert len(theirs) == 1, "the user's NSL row must be untouched"
    assert theirs[0]["AppName"] == "NSL Battle.net"


def test_remove_game_ignores_a_foreign_appid_collision(tmp_path) -> None:
    """``remove_game`` deletes by appid; it must still check ownership."""
    svc = _service(tmp_path)
    collided = -636363
    svc._shortcuts = {"shortcuts": {"0": _rom(collided, "/roms/gba/Metroid.gba")}}
    svc._shortcuts_loaded = True
    svc._games_map = {}
    svc._games_map_loaded = True

    removed = asyncio.run(svc.remove_game(collided))

    assert removed is False
    assert "0" in svc._shortcuts["shortcuts"]
