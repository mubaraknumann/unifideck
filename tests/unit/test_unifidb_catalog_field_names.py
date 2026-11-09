"""Guard test — catalog rows are read under the names the catalog uses.

The unifiDB catalog is IGDB-shaped: ``summary`` and a ``publishers`` list.
``game_to_cache_format`` read ``description`` and ``publisher``, which no row
has, so every game in a measured 1307-game library had an empty synopsis and
publisher. Games with a Steam match hid it (the Steam half supplies both);
Battle.net, GameVault and other games with no Steam app showed nothing.

The rows below are the real shape, copied from the catalog's ``games/`` shards.
"""

from __future__ import annotations

from unifideck.metadata.unifidb import game_to_cache_format
from unifideck.services.metadata_completeness import (
    has_complete_metadata,
    metadata_negative_stale,
    negative_marker,
)

_CATALOG_ROW = {
    "igdb_id": 135243,
    "name": "Alan Wake II",
    "summary": "A successful writer of psychological thrillers...",
    "genres": ["Shooter", "Adventure"],
    "developers": ["Remedy Entertainment"],
    "publishers": ["Epic Games Publishing", "Remedy Entertainment"],
    "aggregated_rating": 90.0,
    "release_date": 1698278400,
    "platforms": ["PC (Microsoft Windows)"],
    "cover_url": "",
    "external_ids": {},
}


def test_summary_becomes_the_description() -> None:
    out = game_to_cache_format(_CATALOG_ROW)
    assert out["description"] == _CATALOG_ROW["summary"]


def test_publishers_list_becomes_the_publisher() -> None:
    out = game_to_cache_format(_CATALOG_ROW)
    assert out["publisher"] == "Epic Games Publishing, Remedy Entertainment"


def test_the_old_names_still_win_when_present() -> None:
    row = dict(_CATALOG_ROW, description="Desc", publisher="Pub")
    out = game_to_cache_format(row)
    assert out["description"] == "Desc"
    assert out["publisher"] == "Pub"


def test_a_row_with_neither_yields_empty_strings() -> None:
    out = game_to_cache_format({"name": "X"})
    assert out["description"] == ""
    assert out["publisher"] == ""


# ── negative markers are about a title ──────────────────────────────

class _Cache:
    def __init__(self, data: dict[tuple[str, str], object]) -> None:
        self._data = data

    def get(self, ns: str, key: str) -> object:
        return self._data.get((ns, key))


class _Game:
    store = "gamevault"
    store_game_id = "lv_1"
    app_id = -5
    title = "Alan Wake 2"


def test_metadata_negative_stale() -> None:
    assert not metadata_negative_stale(negative_marker("Alan Wake 2"), "Alan Wake 2")
    assert metadata_negative_stale(
        negative_marker("Alan Wake 2 AnkerGames"), "Alan Wake 2",
    )
    assert metadata_negative_stale({"_negative": True}, "Alan Wake 2")
    assert not metadata_negative_stale({"genres": []}, "Alan Wake 2")
    assert not metadata_negative_stale(None, "Alan Wake 2")


def test_a_negative_for_an_old_title_is_not_complete() -> None:
    cache = _Cache({
        ("metadata", "gamevault:lv_1"): negative_marker("Alan Wake 2 AnkerGames"),
    })
    assert has_complete_metadata(cache, _Game()) is False  # type: ignore[arg-type]
