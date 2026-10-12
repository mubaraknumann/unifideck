"""Store catalog release dates reach the metadata display, behind real sources.

The catalog date is all a store has when upstream metadata returns nothing
useful, so it has to survive the cache. The negative entry written by a failed
lookup is the case that hid it, and a date Steam or UnifiDB actually supplies
still has to win over the catalog's.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from unifideck.core.types import Game
from unifideck.rpc.mixins._metadata_display import pick_release_date
from unifideck.services import metadata_sources
from unifideck.services.metadata_service import MetadataService


def _service() -> MetadataService:
    svc = MetadataService.__new__(MetadataService)
    svc._cache = type("C", (), {"get": lambda *a, **k: None, "set": lambda *a, **k: None})()
    svc._config = None
    return svc


def _game(meta: dict) -> Game:
    return Game(app_id=0, store="gamevault", store_game_id="1", title="X", metadata=meta)


async def _enrich(meta: dict, steam: dict, unifidb: dict) -> dict:
    with (
        patch.object(metadata_sources, "fetch_steam_store", AsyncMock(return_value=steam)),
        patch.object(metadata_sources, "fetch_unifidb", AsyncMock(return_value=unifidb)),
    ):
        return await _service().enrich(_game(meta))


async def test_catalog_date_fills_a_missing_release_date() -> None:
    merged = await _enrich({"release_date": "2000-01-01"}, {}, {})
    assert pick_release_date({}, merged) == "2000-01-01"


async def test_supplied_enrichment_wins() -> None:
    merged = await _enrich({"release_date": "2000-01-01"}, {}, {"release_date": "1999-05-05"})
    assert merged["release_date"] == "1999-05-05"


async def test_no_catalog_date_supplies_none() -> None:
    merged = await _enrich({}, {}, {})
    assert pick_release_date({}, merged) == ""


def _service_with_cache(entry: dict) -> MetadataService:
    svc = MetadataService.__new__(MetadataService)
    svc._cache = type("C", (), {"get": lambda *a, **k: entry, "set": lambda *a, **k: None})()
    svc._config = None
    return svc


async def test_catalog_date_survives_a_cached_negative_entry() -> None:
    merged = await _service_with_cache({"_negative": True, "title": "X"}).enrich(
        _game({"release_date": "2000-01-01"}),
    )
    assert pick_release_date({}, merged) == "2000-01-01"


async def test_cached_date_wins_over_the_catalog() -> None:
    from unifideck.services import metadata_service

    entry = {
        "release_date": "1999-05-05",
        metadata_service._SAVEDATA_SCHEMA_KEY: metadata_service._SAVEDATA_SCHEMA,
    }
    merged = await _service_with_cache(entry).enrich(_game({"release_date": "2000-01-01"}))
    assert merged["release_date"] == "1999-05-05"
