"""Unit tests for the per-shortcut library-facet assembler.

Guards the cache-reshape that powers Steam's native Sort/Filters for
non-Steam shortcuts (`get_overview_enrichment`) and the shortcut-keyed
Great-on-Deck compat. Validated against the real cache shapes
(`steam_real_appid` signed keys → `steam_metadata`/`compat` by real
Steam AppID) observed on a live device.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from unifideck.rpc.mixins._compat_payload import compat_category
from unifideck.rpc.mixins._library_facets import (
    build_enrichment_map,
    build_facet_record,
)


def _game(app_id: int, store: str, store_game_id: str) -> Any:
    """Duck-typed stand-in for ``core.types.Game`` (only the fields the
    facet builder reads)."""
    return SimpleNamespace(
        app_id=app_id,
        store=store,
        store_game_id=store_game_id,
    )


class _Store:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data


class _Cache:
    def __init__(self, stores: dict[str, dict[str, Any]]) -> None:
        self._stores = {k: _Store(v) for k, v in stores.items()}


# A shortcut AppID in its signed on-disk form (sync stores signed);
# Steam hands the frontend the unsigned form.
_SHORTCUT_SIGNED = -1514014196
_SHORTCUT_UNSIGNED = _SHORTCUT_SIGNED + 0x100000000  # 2780953100
_REAL = 1147860


def _cache_with(**overrides: dict[str, Any]) -> _Cache:
    base = {
        "steam_real_appid": {str(_SHORTCUT_SIGNED): _REAL},
        "steam_metadata": {
            str(_REAL): {
                "name": "20 Minutes Till Dawn",
                "metacritic": {"score": 81},
                "categories": [
                    {"id": 2, "description": "Single-player"},
                    {"id": 1, "description": "Multi-player"},
                ],
                "genres": [
                    {"id": 1, "description": "Action"},
                    {"id": 23, "description": "Indie"},
                ],
                "release_date": {"coming_soon": False, "date": "21 Jun, 2022"},
                "recommendations": {"total": 41234},
            },
        },
        "compat": {
            str(_REAL): {
                "protondb_tier": "platinum",
                "deck_status": "verified",
                "deck_category": 3,
                "machine_status": "playable",
                "machine_category": 2,
            },
        },
        "steam_reviews": {
            str(_REAL): {"review_score": 8, "review_percentage": 95},
        },
        "shortcut_added": {str(_SHORTCUT_SIGNED): 1690000000},
    }
    base.update(overrides)
    return _Cache(base)


def test_build_enrichment_map_emits_both_appid_forms() -> None:
    m = build_enrichment_map(_cache_with())
    assert str(_SHORTCUT_SIGNED) in m
    assert str(_SHORTCUT_UNSIGNED) in m
    # Both forms point at the same record.
    assert m[str(_SHORTCUT_SIGNED)] == m[str(_SHORTCUT_UNSIGNED)]


def test_facet_fields_assembled_from_caches() -> None:
    rec = build_enrichment_map(_cache_with())[str(_SHORTCUT_UNSIGNED)]
    assert rec["steam_app_id"] == _REAL
    assert rec["metacritic"] == 81
    assert rec["release_date"] == "21 Jun, 2022"
    assert rec["recommendations_total"] == 41234
    assert rec["review_score"] == 8
    assert rec["review_percentage"] == 95
    assert rec["date_added_unix"] == 1690000000
    # Resolved for the running device (a Deck in CI/dev).
    assert rec["compat_category"] == 3  # verified
    assert rec["compat_status"] == "verified"
    # Every track rides along for the packed bitfield, unresolved.
    assert rec["compat_categories"]["deck"] == 3
    assert rec["compat_categories"]["machine"] == 2
    assert rec["store_category"] == [2, 1]
    assert rec["store_tag"] == [1, 23]
    assert rec["protondb_tier"] == "platinum"


def test_cold_cache_degrades_to_empty() -> None:
    assert build_enrichment_map(_Cache({})) == {}


def test_missing_reviews_and_date_added_are_none_zero() -> None:
    rec = build_enrichment_map(
        _cache_with(steam_reviews={}, shortcut_added={}),
    )[str(_SHORTCUT_UNSIGNED)]
    assert rec["review_score"] is None
    assert rec["review_percentage"] is None
    assert rec["date_added_unix"] == 0


def test_deck_ladder_is_unchanged_by_the_track_parameter() -> None:
    """The Deck path must behave exactly as it did before tracks existed.

    These are the original assertions verbatim, with the track passed
    explicitly. If any of them move, the change stopped being a rename.
    """
    deck = lambda e: compat_category(e, "deck")  # noqa: E731
    # Valve "unknown" but ProtonDB platinum/native → Playable (2).
    assert deck({"deck_status": "unknown", "protondb_tier": "platinum"}) == 2
    assert deck({"deck_status": "", "protondb_tier": "native"}) == 2
    # Valve rating always wins when present.
    assert deck({"deck_status": "verified", "protondb_tier": "gold"}) == 3
    assert deck({"deck_status": "playable"}) == 2
    # No signal → Unknown (0); a low ProtonDB tier is NOT optimistic.
    assert deck({"protondb_tier": "gold"}) == 0
    assert deck({}) == 0


def test_ladder_reads_the_requested_track_only() -> None:
    """Valve rates each device independently — no cross-device borrowing."""
    entry = {
        "deck_category": 3, "deck_status": "verified",
        "machine_category": 0, "machine_status": "unknown",
    }
    assert compat_category(entry, "deck") == 3
    # Machine is unrated and there is no ProtonDB tier to promote it, so
    # it must NOT inherit the Deck's verdict.
    assert compat_category(entry, "machine") == 0


def test_machine_track_promotes_independently() -> None:
    """The measured case: Playable on Deck, Verified on Machine."""
    entry = {
        "deck_category": 2, "deck_status": "playable",
        "machine_category": 3, "machine_status": "verified",
    }
    assert compat_category(entry, "deck") == 2
    assert compat_category(entry, "machine") == 3


def test_warm_cache_without_category_ints_still_rates() -> None:
    """Entries predating the per-track ints carry only a status string.

    They must keep rating until the schema self-heal rewrites them,
    or a warm Deck reads as Unknown for its whole library at startup.
    """
    assert compat_category({"deck_status": "verified"}, "deck") == 3
    assert compat_category({"deck_status": "unsupported"}, "deck") == 1
    assert compat_category({"steamos_status": "compatible"}, "steamos") == 2


def test_unmapped_shortcut_absent_from_map() -> None:
    # A shortcut with no steam_real_appid mapping yields no facet.
    cache = _cache_with(steam_real_appid={})
    assert build_enrichment_map(cache) == {}


def test_build_facet_record_direct() -> None:
    cache = _cache_with()
    rec = build_facet_record(
        cache,
        _SHORTCUT_SIGNED,
        _REAL,
        reviews_data={str(_REAL): {"review_score": 9, "review_percentage": 98}},
        added_data={str(_SHORTCUT_SIGNED): 1700000000},
        meta_entry=None,
    )
    assert rec["review_score"] == 9
    assert rec["date_added_unix"] == 1700000000


def test_metacritic_from_store_gid_entry_via_games() -> None:
    # Steam appdetails lacks a metacritic score, but the metacritic.com
    # backfill wrote one into ``metadata[store:game_id]`` WITHOUT a
    # steam_appid (the orphaned-score case that the old steam_appid
    # composite join dropped). The games path joins by store:game_id and
    # recovers it.
    cache = _cache_with(
        steam_metadata={str(_REAL): {"name": "Alex Kidd"}},  # no "metacritic"
        metadata={"epic:abc": {"title": "Alex Kidd", "metacritic_score": 65}},
    )
    games = [_game(_SHORTCUT_SIGNED, "epic", "abc")]
    rec = build_enrichment_map(cache, games)[str(_SHORTCUT_UNSIGNED)]
    assert rec["metacritic"] == 65


def test_steam_metacritic_wins_over_store_gid_entry() -> None:
    cache = _cache_with(
        metadata={"epic:abc": {"metacritic_score": 50}},
    )
    # _cache_with default steam_metadata has metacritic.score == 81.
    games = [_game(_SHORTCUT_SIGNED, "epic", "abc")]
    rec = build_enrichment_map(cache, games)[str(_SHORTCUT_UNSIGNED)]
    assert rec["metacritic"] == 81


def test_games_path_record_without_resolved_real_appid() -> None:
    # A game whose real Steam AppID never resolved (no steam_real_appid
    # entry) is SKIPPED by the cache-only fallback, but the games path
    # still emits a facet — with metacritic from store:game_id — so it
    # sorts correctly instead of dropping to "Everything Else".
    cache = _cache_with(
        steam_real_appid={},
        steam_metadata={},
        metadata={"gog:xyz": {"metacritic_score": 77}},
    )
    games = [_game(_SHORTCUT_SIGNED, "gog", "xyz")]
    out = build_enrichment_map(cache, games)
    rec = out[str(_SHORTCUT_UNSIGNED)]
    assert rec["steam_app_id"] == 0
    assert rec["metacritic"] == 77
    # Both signed + unsigned forms still emitted.
    assert str(_SHORTCUT_SIGNED) in out


def test_games_path_falls_back_to_real_appid_enumeration() -> None:
    # No games supplied (sync service unavailable) → enumerate
    # steam_real_appid; metacritic comes only from Steam's appdetails.
    rec = build_enrichment_map(_cache_with(), None)[str(_SHORTCUT_UNSIGNED)]
    assert rec["metacritic"] == 81

def test_deck_ladder_matches_the_pre_track_implementation_exhaustively() -> None:
    """Sweep every (deck_status, protondb_tier) pair the cache can hold.

    The pre-change ladder is inlined here rather than imported, because the
    point is to pin behaviour that no longer exists in the tree. Verified
    against this developer's real 1000-entry warm cache at the time of the
    change: zero mismatches.
    """

    def pre_change_ladder(entry: dict[str, Any]) -> int:
        status = str(entry.get("deck_status", "")).lower()
        category = {"verified": 3, "playable": 2, "unsupported": 1}.get(status, 0)
        if category == 0:
            tier = str(entry.get("protondb_tier", "")).lower()
            if tier in ("platinum", "native"):
                return 2
        return category

    statuses = ["verified", "playable", "unsupported", "unknown", "", None]
    tiers = ["platinum", "native", "gold", "silver", "bronze", "borked",
             "pending", "", None]
    for status in statuses:
        for tier in tiers:
            entry: dict[str, Any] = {"deck_status": status, "protondb_tier": tier}
            assert compat_category(entry, "deck") == pre_change_ladder(entry), (
                f"deck ladder changed for status={status!r} tier={tier!r}"
            )
