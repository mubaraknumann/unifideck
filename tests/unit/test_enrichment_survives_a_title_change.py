"""Enrichment must re-derive when a game's title changes.

Both caches guarded here answer a question whose *only* input is the title —
SteamGridDB's search and Steam's ``storesearch`` — yet both were keyed on the
game's identity alone, with nothing recording which title the answer referred
to. A negative result therefore outlived the title it was about, and the game
could never be enriched again.

Found on a real GameVault game. The user's self-hosted server first exposed it
as ``Test Game 2026``:

    [ArtworkService] fetching art for Test Game 2026 (need: grid+grid_l+hero+icon+logo)
    [ArtworkService] artwork batch finished: 0 covers saved, ... 95 no match

Once they curated the entry it became ``Ghost of Tsushima`` — same server id,
same shortcut AppID (``shortcuts_registry.json`` pins one per ``store:game_id``)
— and from then on every sync skipped it:

    artwork_attempts:  gamevault:1  → ['grid', 'grid_l', 'hero', 'icon', 'logo']
    steam_real_appid:  -982520195   → -1

The first record made the artwork phase skip the game while reporting it as
"no match" (a skip returns all-False, which ``_fetch_one`` cannot tell from a
genuine miss). The second made the ProtonDB / Deck-Verified badge read
"UNKNOWN" for a game with thousands of ProtonDB reports, because that tier
hangs off a real Steam AppID.

Nothing about this is GameVault-specific — it is simply the first store where
the title comes from the user's own data and so changes in normal use. An Epic
edition rename or a GOG re-release strands a game exactly the same way.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.services.metadata_steam_mixin import (
    STEAM_APPID_MISS_NS,
    steam_appid_miss_stale,
)


class _Cache:
    """Namespace/key store with the two methods these paths use."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get(self, ns: str, key: str) -> Any:
        return self.data.get((ns, key))

    def set(self, ns: str, key: str, value: Any, flush: bool = True) -> None:
        self.data[(ns, key)] = value


# ── the Steam-AppID miss ───────────────────────────────────────────────

def test_a_miss_is_final_for_the_title_it_searched() -> None:
    cache = _Cache()
    cache.set(STEAM_APPID_MISS_NS, "-982520195", "Test Game 2026")

    assert not steam_appid_miss_stale(cache, -982520195, "Test Game 2026")


def test_a_miss_does_not_survive_the_title_it_was_about() -> None:
    """The regression: ``-1`` for one title must not answer for another."""
    cache = _Cache()
    cache.set(STEAM_APPID_MISS_NS, "-982520195", "Test Game 2026")

    assert steam_appid_miss_stale(cache, -982520195, "Ghost of Tsushima")


def test_a_miss_recorded_before_this_cache_existed_is_stale() -> None:
    """Self-healing: no recorded title means re-search once, then record."""
    assert steam_appid_miss_stale(_Cache(), -982520195, "Ghost of Tsushima")


# ── the artwork attempts cache ─────────────────────────────────────────

@pytest.fixture
def artwork_service() -> Any:
    """An ArtworkService with only the fields the skip decision touches."""
    from unifideck.services.artwork.service import ArtworkService

    svc = ArtworkService.__new__(ArtworkService)
    svc._cache = _Cache()  # type: ignore[attr-defined]
    return svc


_ALL_KINDS = {"grid", "grid_l", "hero", "icon", "logo"}


def test_the_same_gaps_under_the_same_title_are_skipped(artwork_service: Any) -> None:
    """The optimisation still works: don't re-query art that isn't there."""
    from unifideck.services.artwork.service import _ATTEMPTS_NAMESPACE

    artwork_service._cache.set(
        _ATTEMPTS_NAMESPACE,
        "gamevault:1",
        {"missing": sorted(_ALL_KINDS), "title": "Test Game 2026"},
    )

    assert artwork_service._missing_set_unchanged(
        "gamevault:1", _ALL_KINDS, False, "Test Game 2026",
    )


def test_the_same_gaps_under_a_new_title_are_a_new_question(
    artwork_service: Any,
) -> None:
    """The regression: SGDB is asked about a title, so a new title must ask."""
    from unifideck.services.artwork.service import _ATTEMPTS_NAMESPACE

    artwork_service._cache.set(
        _ATTEMPTS_NAMESPACE,
        "gamevault:1",
        {"missing": sorted(_ALL_KINDS), "title": "Test Game 2026"},
    )

    assert not artwork_service._missing_set_unchanged(
        "gamevault:1", _ALL_KINDS, False, "Ghost of Tsushima",
    )


def test_a_pre_fix_list_record_is_retried_once(artwork_service: Any) -> None:
    """Records written before the fix are bare lists — treat as changed."""
    from unifideck.services.artwork.service import _ATTEMPTS_NAMESPACE

    artwork_service._cache.set(
        _ATTEMPTS_NAMESPACE, "gamevault:1", sorted(_ALL_KINDS),
    )

    assert not artwork_service._missing_set_unchanged(
        "gamevault:1", _ALL_KINDS, False, "Ghost of Tsushima",
    )


def test_force_never_reports_unchanged(artwork_service: Any) -> None:
    from unifideck.services.artwork.service import _ATTEMPTS_NAMESPACE

    artwork_service._cache.set(
        _ATTEMPTS_NAMESPACE,
        "gamevault:1",
        {"missing": sorted(_ALL_KINDS), "title": "Ghost of Tsushima"},
    )

    assert not artwork_service._missing_set_unchanged(
        "gamevault:1", _ALL_KINDS, True, "Ghost of Tsushima",
    )


# ── the size lookup ────────────────────────────────────────────────────

def test_every_store_with_a_real_get_game_size_is_size_capable() -> None:
    """GameVault shipped absent from this list and showed "Size: 0 MB".

    Not derivable from the code — ``get_game_size`` is abstract, so every
    store overrides it and Ubisoft's body is ``return None``. The list is
    written by hand, so it gets a test.
    """
    from unifideck.services.size_backfill import SIZE_CAPABLE_STORES

    assert SIZE_CAPABLE_STORES == {"epic", "gog", "amazon", "gamevault"}
