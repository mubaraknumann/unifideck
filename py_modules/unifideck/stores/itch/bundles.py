"""itch.io bundle games: paid for through a bundle, not yet claimed.

Buying a bundle does not put its games in the itch.io library. Each one
becomes an owned key only when the user claims it on the bundle's download
page, so ``Fetch.ProfileOwnedKeys`` never lists the rest. A tester who
bought the ~1,700-game Bundle for Racial Justice and Equality had 96 owned
keys, and the bundle's games were missing from Steam (2026-10).

butler 15.31 lists them (``endpoints/fetch/fetch_profilebundleownerships.go``):

* ``Fetch.ProfileBundleOwnerships {fresh}`` refreshes the owned-bundles list
  (one API call) and re-walks only the bundles whose game list is stale.
  butler keeps a bundle's games for 7 days and drops them early when itch.io
  bumps the bundle's ``version``, so after the first sync this is cheap.
* ``Fetch.ProfileOwnedBundles`` and ``Fetch.BundleGames`` then read butler's
  database without ``fresh``.

The user decided that bundle games are listed, and that the itch.io claim
page opens before one installs (the claim is theirs to make, not a silent
API call).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: ``ItchLibraryReader._paged``: every page of a cursor-paged butlerd fetch.
Pager = Callable[..., Awaitable[list[dict[str, Any]]]]
#: ``ItchLibraryReader._call_with_backoff``.
Caller = Callable[..., Awaitable[dict[str, Any]]]

#: The first walk of a large bundle is one API page per ~50 games.
_OWNERSHIPS_TIMEOUT_S = 600.0


async def read_bundle_games(
    paged: Pager, call: Caller, profile: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(game, bundle)`` for every game in every bundle the profile owns.

    Only ``classification: "game"`` rows (butler filters them). A game in
    two bundles is listed once, with the first bundle.
    """
    await call(
        "Fetch.ProfileBundleOwnerships", {"profileId": profile, "fresh": True},
        call_timeout_s=_OWNERSHIPS_TIMEOUT_S,
    )
    found: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    for key in await paged("Fetch.ProfileOwnedBundles", {"profileId": profile}, fresh=False):
        bundle = key.get("bundle") or {}
        if not bundle.get("id"):
            continue
        items = await paged("Fetch.BundleGames", {
            "profileId": profile, "bundleId": bundle["id"],
            "filters": {"classification": "game"},
        }, fresh=False)
        for item in items:
            game = item.get("game")
            if isinstance(game, dict) and game.get("id"):
                found.setdefault(game["id"], (game, bundle))
    return list(found.values())
