"""Force sync = full re-pull: cached entries bypassed and overwritten.

``is_force`` (the Force Sync button) previously never reached
MetadataService or CompatibilityService — a force sync could not
refresh anything already cached. Now ``force=True`` bypasses cache
reads (negative markers included), re-resolves title → AppID
matches, and overwrites entries on completion; standard sync keeps
skipping cached games.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from unifideck.compatibility.deck_verified import (
    TRACK_NAMES,
    TrackResult,
    spec_for,
)
from unifideck.compatibility.library import CompatLibrary
from unifideck.services.metadata_service import MetadataService


def _tracks(**kw: int) -> dict[str, TrackResult]:
    """Build a fetch result. ``_tracks(deck=2)`` -> Deck Playable."""
    out = {n: TrackResult() for n in TRACK_NAMES}
    for name, category in kw.items():
        statuses = spec_for(name).statuses  # type: ignore[union-attr]
        out[name] = TrackResult(
            category=category, status=statuses.get(category, "unknown"),
        )
    return out



class _Store:
    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def get(self, k: str, default: object = None) -> object:
        return self._data.get(k, default)

    def set(self, k: str, v: object) -> None:
        self._data[k] = v


class _Cache:
    def __init__(self) -> None:
        self._stores: dict[str, _Store] = {}

    def register(self, *a: object, **k: object) -> None:
        pass

    def get(self, ns: str, k: str) -> object:
        return self._stores.setdefault(ns, _Store()).get(k)

    def set(
        self, ns: str, k: str, v: object, *, flush: bool = True,
    ) -> None:
        self._stores.setdefault(ns, _Store()).set(k, v)

    def flush(self, ns: str) -> None:
        pass


class _Bus:
    def on(self, *a: object, **k: object) -> None:
        pass


def _game(app_id: int, store: str, game_id: str, title: str) -> Any:
    return SimpleNamespace(
        app_id=app_id, store=store, store_game_id=game_id, title=title,
    )


def _metadata_service(cache: _Cache) -> MetadataService:
    return MetadataService(bus=_Bus(), cache=cache)  # type: ignore[arg-type]


_GAME = _game(-1514014196, "epic", "amongus", "Among Us")


# ── enrich(): cached vs force ─────────────────────────────────────


@pytest.mark.asyncio
async def test_standard_enrich_returns_cached_without_fetching() -> None:
    """A standard enrich serves the cache and never re-fetches Steam.

    The Steam source is the expensive one (an API call per game), and this is
    the guarantee that keeps a routine sync cheap.

    It may still top up the unifiDB save-location block on an entry written
    before that block was carried through — see ``_served_from_cache``. That
    is bucket-cached and bounded to once per entry, so it is mocked out here
    rather than asserted against; ``test_metadata_savedata_cache_topup.py``
    covers it directly.
    """
    cache = _Cache()
    cache.set("metadata", "epic:amongus", {"description": "old"})
    svc = _metadata_service(cache)
    steam = AsyncMock(return_value={"description": "new"})
    with (
        patch(
            "unifideck.services.metadata_sources.fetch_steam_store", new=steam,
        ),
        patch(
            "unifideck.services.metadata_sources.fetch_unifidb",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await svc.enrich(_GAME)
    steam.assert_not_called()
    assert result["description"] == "old"


@pytest.mark.asyncio
async def test_force_enrich_refetches_and_overwrites_cached() -> None:
    cache = _Cache()
    cache.set("metadata", "epic:amongus", {"description": "old"})
    svc = _metadata_service(cache)
    with (
        patch(
            "unifideck.services.metadata_sources.fetch_steam_store",
            new=AsyncMock(return_value={"description": "new"}),
        ),
        patch(
            "unifideck.services.metadata_sources.fetch_unifidb",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await svc.enrich(_GAME, force=True)
    assert result == {"description": "new"}
    assert cache._stores["metadata"].get("epic:amongus") == {"description": "new"}


@pytest.mark.asyncio
async def test_force_enrich_retries_negative_marker() -> None:
    cache = _Cache()
    cache.set("metadata", "epic:amongus", {"_negative": True})
    svc = _metadata_service(cache)
    with (
        patch(
            "unifideck.services.metadata_sources.fetch_steam_store",
            new=AsyncMock(return_value={"description": "found now"}),
        ),
        patch(
            "unifideck.services.metadata_sources.fetch_unifidb",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await svc.enrich(_GAME, force=True)
    assert result == {"description": "found now"}


# ── _resolve_steam_id(): force re-resolution ──────────────────────


@pytest.mark.asyncio
async def test_force_resolve_re_searches_despite_positive_cache() -> None:
    # User decision: force sync re-resolves EVERYTHING, fixing
    # wrong-match cases without manual intervention.
    cache = _Cache()
    cache.set("steam_real_appid", str(_GAME.app_id), 111)
    svc = _metadata_service(cache)
    search = AsyncMock(return_value={"app_id": 945360})
    with patch("unifideck.steam.library.search_store", new=search):
        steam_id = await svc._resolve_steam_id(_GAME, None, force=True)
    search.assert_awaited_once()
    assert steam_id == 945360


@pytest.mark.asyncio
async def test_force_resolve_retries_negative_mapping() -> None:
    cache = _Cache()
    cache.set("steam_real_appid", str(_GAME.app_id), -1)
    svc = _metadata_service(cache)
    with patch(
        "unifideck.steam.library.search_store",
        new=AsyncMock(return_value={"app_id": 945360}),
    ):
        steam_id = await svc._resolve_steam_id(_GAME, None, force=True)
    assert steam_id == 945360


@pytest.mark.asyncio
async def test_force_resolve_keeps_positive_mapping_on_empty_search() -> None:
    # An empty storesearch is indistinguishable from a transient
    # failure — a forced re-resolve must not wipe good mappings.
    cache = _Cache()
    cache.set("steam_real_appid", str(_GAME.app_id), 945360)
    svc = _metadata_service(cache)
    with patch(
        "unifideck.steam.library.search_store",
        new=AsyncMock(return_value=None),
    ):
        steam_id = await svc._resolve_steam_id(_GAME, None, force=True)
    assert steam_id == 945360


@pytest.mark.asyncio
async def test_standard_resolve_still_uses_cache_without_searching() -> None:
    cache = _Cache()
    cache.set("steam_real_appid", str(_GAME.app_id), 945360)
    svc = _metadata_service(cache)
    search = AsyncMock()
    with patch("unifideck.steam.library.search_store", new=search):
        steam_id = await svc._resolve_steam_id(_GAME, None)
    search.assert_not_called()
    assert steam_id == 945360


# ── compat refresh ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_for_appid_refresh_bypasses_and_overwrites() -> None:
    cache = _Cache()
    cache.set("compat", "945360", {
        "deck_status": "playable",
        "deck_test_results": [{"text": "old", "passed": True}],
        "dtr_checked": True,
    })
    lib = CompatLibrary(cache=cache)  # type: ignore[arg-type]
    with (
        patch.object(lib, "_fetch_protondb", new=AsyncMock(return_value="gold")),
        patch.object(
            lib, "_fetch_compat",
            new=AsyncMock(return_value=_tracks(deck=3)),
        ),
    ):
        rating = await lib.get_for_appid(945360, refresh=True)
    assert rating.deck_status == "verified"
    assert rating.protondb_tier == "gold"
    stored = cache._stores["compat"].get("945360")
    assert isinstance(stored, dict) and stored["deck_status"] == "verified"


@pytest.mark.asyncio
async def test_get_for_appid_without_refresh_uses_cache() -> None:
    cache = _Cache()
    cache.set("compat", "945360", {
        "deck_status": "playable",
        "deck_test_results": [{"text": "ok", "passed": True}],
        "dtr_checked": True,
    })
    lib = CompatLibrary(cache=cache)  # type: ignore[arg-type]
    protondb = AsyncMock()
    with patch.object(lib, "_fetch_protondb", new=protondb):
        rating = await lib.get_for_appid(945360)
    protondb.assert_not_called()
    assert rating.deck_status == "playable"
