"""Regression: compat backfills the shortcut → real-Steam-AppID mapping.

Compat resolves a Steam AppID by title even for games the metadata
phase negative-cached or never resolved (e.g. "Among Us": ProtonDB
data exists under appid 945360, but the shortcut had no
``steam_real_appid`` mapping, so the library-facets join never
surfaced the badge). ``get_for_title`` must persist that mapping when
it resolves via ``search_store``, so the badge appears.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from unifideck.compatibility.deck_verified import (
    TRACK_NAMES,
    TrackResult,
    spec_for,
)
from unifideck.compatibility.library import CompatLibrary


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
        self._stores: dict[str, _Store] = {
            "compat": _Store(),
            "steam_real_appid": _Store(),
        }

    def register(self, *a: object, **k: object) -> None:
        pass

    def get(self, ns: str, k: str) -> object:
        return self._stores.setdefault(ns, _Store()).get(k)

    def set(self, ns: str, k: str, v: object) -> None:
        self._stores.setdefault(ns, _Store()).set(k, v)


_SHORTCUT = -1514014196
_REAL = 945360


@pytest.mark.asyncio
async def test_get_for_title_backfills_steam_real_appid() -> None:
    cache = _Cache()
    lib = CompatLibrary(cache=cache)
    with (
        patch(
            "unifideck.steam.library.search_store",
            new=AsyncMock(return_value={"app_id": _REAL, "name": "Among Us"}),
        ),
        patch.object(lib, "_fetch_protondb", new=AsyncMock(return_value="gold")),
        patch.object(
            lib, "_fetch_compat", new=AsyncMock(return_value=_tracks(deck=2)),
        ),
    ):
        rating = await lib.get_for_title("Among Us", shortcut_app_id=_SHORTCUT)

    assert rating.protondb_tier == "gold"
    assert rating.deck_status == "playable"
    # The shortcut → AppID mapping is now persisted for the facet join.
    assert cache._stores["steam_real_appid"].get(str(_SHORTCUT)) == _REAL


@pytest.mark.asyncio
async def test_cached_appid_path_does_not_need_backfill() -> None:
    # When the mapping already exists (metadata resolved it), compat
    # uses it directly and doesn't re-search.
    cache = _Cache()
    cache.set("steam_real_appid", str(_SHORTCUT), _REAL)
    lib = CompatLibrary(cache=cache)
    search = AsyncMock(return_value=None)
    with (
        patch("unifideck.steam.library.search_store", new=search),
        patch.object(lib, "_fetch_protondb", new=AsyncMock(return_value="platinum")),
        patch.object(
            lib, "_fetch_compat", new=AsyncMock(return_value=_tracks(deck=3)),
        ),
    ):
        rating = await lib.get_for_title("Among Us", shortcut_app_id=_SHORTCUT)

    assert rating.deck_status == "verified"
    search.assert_not_called()  # cached mapping → no live search
