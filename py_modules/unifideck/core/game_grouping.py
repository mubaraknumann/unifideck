"""Cross-store duplicate-card grouping — display layer only.

py_modules/unifideck/core/game_grouping.py

Unlike :mod:`cross_source_dedupe` (which *collapses* duplicates into a
single shortcut, disabled by default), this module never removes or
reorders a game. It stamps these display fields on each
:class:`~unifideck.core.types.Game`:

* ``dedupe_group_id``: shared by every library copy of the same game,
  ``None`` when this is the only copy.
* ``edition_label``: the edition or variant named in this copy's title.
* ``steam_owned_app_id`` / ``steam_owned_edition_label``: the owned Steam
  app that is this copy's own version, and the edition in its Steam title.
* ``steam_versions``: every owned Steam version of the game, so the store
  switcher lists them all and the library shows one Steam tile for them.

Which rows are "the same game" is :mod:`unifideck.core.game_identity`'s
answer, the same one the Steam Store ownership ribbon uses. The frontend
reads these fields for the "Group duplicates" library tabs and the store
switcher on a game page (``src/lib/library-filters``).

Wiring: after a sync (``sync_results_mixin``), on cache load and after the
metadata phase (``sync_cache_mixin``), and when the frontend pushes the
owned Steam library (``rpc.mixins.sync.update_steam_owned_titles``).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from unifideck.core.game_identity import SteamApp, build_identity
from unifideck.core.steam_appid_map import read_positive_steam_appid
from unifideck.utils.title_match import extract_edition_label

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

    from .types import Game

logger = logging.getLogger(__name__)


def clear_duplicate_groups(games: Iterable[Game]) -> None:
    """Reset the grouping fields on every game."""
    for game in games:
        game.dedupe_group_id = None
        game.edition_label = None
        game.steam_owned_app_id = None
        game.steam_owned_edition_label = None
        game.steam_versions = []


def annotate_duplicate_groups(
    games: Sequence[Game],
    *,
    steam_appid_of: Callable[[Game], int | None] | None = None,
    steam_owned: Iterable[SteamApp] = (),
) -> list[Game]:
    """Stamp the grouping fields on every game, in place.

    Every field is reset first, so a match that no longer holds (the rule
    changed, the Steam library changed, a mapping was corrected) never
    survives a re-annotation.

    Args:
        games: the library. Same objects, same order, are returned.
        steam_appid_of: the row's mapped real Steam AppID, or ``None``.
        steam_owned: the user's owned Steam games.
    """
    games = list(games)
    clear_duplicate_groups(games)
    appids = [steam_appid_of(g) if steam_appid_of else None for g in games]
    identity = build_identity([g.title or "" for g in games], appids, steam_owned)
    for index, game in enumerate(games):
        game.edition_label = extract_edition_label(game.title or "")
        group = identity.group_of[index]
        if identity.library_count(group) > 1:
            game.dedupe_group_id = group
        steam = identity.steam_for[index]
        if steam is not None:
            game.steam_owned_app_id = steam.appid
            game.steam_owned_edition_label = extract_edition_label(steam.title)
        game.steam_versions = [
            {"app_id": app.appid, "edition_label": extract_edition_label(app.title)}
            for app in identity.steam_versions(group)
        ]
    return games


def annotate_duplicate_groups_if_enabled(
    games: list[Game], config: ConfigManager | None, cache: Any = None,
) -> list[Game]:
    """:func:`annotate_duplicate_groups` with the real inputs, gated.

    ``dedup.ui_grouping_enabled`` (default true) off clears every field, so
    turning it off removes a bad grouping instead of freezing it.

    Args:
        games: the library, annotated in place.
        config: the plugin config (gate and Steam path).
        cache: the ``CacheManager`` holding ``steam_real_appid``. ``None``
            groups by title alone.
    """
    from unifideck.utils.config_helpers import get_cfg

    if not get_cfg(config, "dedup.ui_grouping_enabled", True):
        clear_duplicate_groups(games)
        return games
    return annotate_duplicate_groups(
        games,
        steam_appid_of=lambda g: read_positive_steam_appid(cache, g.app_id) or None,
        steam_owned=load_owned_steam_apps(config),
    )


def load_owned_steam_apps(config: ConfigManager | None) -> list[SteamApp]:
    """The user's owned Steam games: the frontend's full-library push plus
    the installed ones from appmanifests. ``[]`` when neither is readable,
    which only means no Steam cross-reference this time."""
    from unifideck.steam.owned_games import get_all_owned_app_ids, get_owned_app_ids

    try:
        installed = {app.appid for app in get_owned_app_ids(config).values()}
        owned = get_all_owned_app_ids(config).values()
    except Exception:
        logger.exception(
            "[game_grouping] could not read the owned Steam library; "
            "grouping without the Steam cross-reference",
        )
        return []
    return [SteamApp(app.appid, app.title, app.appid in installed) for app in owned]
