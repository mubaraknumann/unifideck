"""
Hide Ubisoft games that are owned on the native Steam library.


A Ubisoft title the user owns on Steam still shows up in UPC, but its
``uplay://`` shortcut is a dead end — the entitlement is bound to the
Steam copy, so the game can only launch from Steam, not the Ubisoft
launcher. Surfacing it as a Unifideck shortcut just produces a
non-launchable entry, so we hide it.

The original ``steam_filter.py`` was removed (commits 6c84e7e / 908d350)
for being flaky. This re-implementation stays conservative but covers two
real cases the exact-only version missed (a Ubisoft game owned on Steam
still showed up because):

* **Owned ≠ installed.** ``appmanifest`` only lists *installed* Steam
  games. The full owned library (installed or not) is enumerated by the
  frontend (``collectionStore``) and pushed into a cache that
  :func:`load_steam_owned_titles` unions in. Without it, a game you own
  on Steam but haven't installed slips through.
* **Name divergence.** Steam and Ubisoft don't agree on titles: Steam
  "Tom Clancy's Rainbow Six Siege" vs Ubisoft "Rainbow Six Siege"
  (publisher prefix), or "Watch_Dogs" vs "Watch Dogs" (the normaliser
  strips the underscore). :func:`_owned_on_steam` adds a length-guarded
  publisher-prefix (suffix) match and a whitespace-insensitive match on
  top of exact equality — kept tight to avoid false-positive hiding.

Safety contract preserved: an empty owned set never hides anything, and
an installed (through us) game is never hidden. Matching is unified on
:func:`normalize_title_for_matching` (the shared cross-store normaliser),
so both sides are normalised identically.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.metadata.unifidb import normalize_title_for_matching

if TYPE_CHECKING:
    from unifideck.core.types import Game

logger = logging.getLogger(__name__)
# Steam ships these as appmanifest entries too; never treat them as games.
_NON_GAME_PREFIXES = (
    "proton",
    "steam linux runtime",
    "steamworks",
    "steamvr",
)
# Min normalised length before the fuzzy (prefix/whitespace) tiers may
# fire — short titles match too eagerly ("far cry" ⊂ "far cry 6").
_MIN_FUZZY_LEN = 8


def load_steam_owned_titles() -> frozenset[str]:
    """Normalised titles of games the user owns on Steam.

    Unions two sources, both already normalised with
    :func:`normalize_title_for_matching`:

    * **installed** games from ``appmanifest_*.acf`` (via
      :func:`unifideck.steam.owned_games.get_owned_titles`), and
    * the **full owned** library pushed by the frontend (via
      :func:`unifideck.steam.owned_games.load_frontend_owned_titles`) —
      this is what covers owned-but-not-installed games.

    Returns an empty set when neither source is available; callers MUST
    treat empty as "don't filter", never "hide everything".
    """
    try:
        from unifideck.steam.owned_games import (
            get_owned_titles,
            load_frontend_owned_titles,
        )
    except ImportError:
        logger.debug("[UbisoftSteamFilter] steam.owned_games unavailable")
        return frozenset()
    titles: set[str] = set()
    try:
        titles.update(get_owned_titles())
    except Exception as e:
        logger.debug("[UbisoftSteamFilter] installed scan failed: %s", e)
    try:
        titles.update(load_frontend_owned_titles())
    except Exception as e:
        logger.debug("[UbisoftSteamFilter] frontend cache read failed: %s", e)
    return frozenset(
        title
        for title in titles
        if title and not title.startswith(_NON_GAME_PREFIXES)
    )


def _owned_on_steam(norm: str, steam_titles: frozenset[str]) -> bool:
    """True if ``norm`` matches a Steam-owned title (exact or guarded fuzzy).

    Tiers (all length-guarded to avoid over-hiding):
    1. exact normalised equality;
    2. publisher-prefix — a Steam title is ``"<prefix> <norm>"`` or vice
       versa ("Tom Clancy's Rainbow Six Siege" ⊃ "Rainbow Six Siege");
    3. whitespace-insensitive equality ("watchdogs" == "watch dogs").
    """
    if not norm:
        return False
    if norm in steam_titles:
        return True
    if len(norm) < _MIN_FUZZY_LEN:
        return False
    tight = norm.replace(" ", "")
    needle = " " + norm
    for s in steam_titles:
        if s.endswith(needle):
            return True
        if len(s) >= _MIN_FUZZY_LEN:
            if norm.endswith(" " + s):
                return True
            if s.replace(" ", "") == tight:
                return True
    return False


def apply_steam_owned_filter(
    games: list[Game],
    steam_titles: frozenset[str],
) -> tuple[list[Game], list[str]]:
    """Drop not-installed Ubisoft games whose title is owned on Steam.

    Returns ``(kept_games, hidden_titles)``. Installed games are always
    kept — we never hide a game the user actually installed through us.
    See the module docstring for the matching/safety rationale.
    """
    if not steam_titles:
        return games, []
    kept: list[Game] = []
    hidden: list[str] = []
    for game in games:
        norm = normalize_title_for_matching(game.title)
        if not game.installed and _owned_on_steam(norm, steam_titles):
            hidden.append(game.title)
            continue
        kept.append(game)
    if hidden:
        logger.info(
            "[UbisoftSteamFilter] hid %d Steam-owned title(s): %s",
            len(hidden),
            ", ".join(sorted(hidden)),
        )
    return kept, hidden
