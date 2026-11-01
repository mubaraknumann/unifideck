"""Cached metadata entries written before the save-block fix top themselves up.

``fetch_unifidb`` used to drop ``save_locations`` / ``cloud`` / ``save_source``
before they reached the metadata cache. Fixing the projection alone was not
enough: the ``metadata`` namespace has a **30-day TTL**, and on a real device
the 1208 cached entries were only 3–10 days old. ``enrich()`` returns a cached
entry early, so a normal library sync would keep serving save-data-less entries
for weeks — the cloud-save button, the save-path resolver and the pre-install
cloud indicator all silently broken, with a *force* sync the only cure and
nothing telling the user that.

So an entry lacking the current schema stamp gets its unifiDB half re-fetched
and merged in place. The Steam half (the expensive part) is never re-fetched.
"""
from __future__ import annotations

import types

import pytest

from unifideck.services import metadata_service
from unifideck.services.metadata_service import MetadataService


class _Cache:
    """Minimal CacheManager stand-in recording reads and writes."""

    def __init__(self, initial: dict | None = None) -> None:
        self.store: dict[str, dict] = dict(initial or {})
        self.writes: list[tuple[str, dict]] = []

    def get(self, _ns: str, key: str):
        return self.store.get(key)

    def set(self, _ns: str, key: str, value: dict, flush: bool = True) -> None:
        self.store[key] = value
        self.writes.append((key, value))


def _service(cache: _Cache) -> MetadataService:
    return MetadataService(bus=types.SimpleNamespace(), cache=cache, config=None)


def _game() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        store="gog", store_game_id="1207666643", title="Bastion",
    )


_LEGACY_ENTRY = {
    "title": "Bastion",
    "description": "An action RPG.",
    "steam_appid": "107100",
}

_FRESH_UNIFIDB = {
    "description": "An action RPG.",
    "save_locations": [{"path": "<winAppData>/Bastion"}],
    "cloud": {"gog": True},
    "save_source": "Ludusavi",
}


@pytest.fixture
def _stub_fetch(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def _install(payload):
        async def fake(game, config=None):
            calls.append(game.title)
            return payload
        monkeypatch.setattr(
            metadata_service.metadata_sources, "fetch_unifidb", fake,
        )
        return calls
    return _install


@pytest.mark.asyncio
async def test_legacy_entry_gains_the_save_block(_stub_fetch) -> None:
    cache = _Cache({"gog:1207666643": dict(_LEGACY_ENTRY)})
    calls = _stub_fetch(_FRESH_UNIFIDB)

    out = await _service(cache).enrich(_game())

    assert out["cloud"] == {"gog": True}
    assert out["save_locations"] == [{"path": "<winAppData>/Bastion"}]
    # Steam-side data preserved — the top-up must not re-fetch or clobber it.
    assert out["steam_appid"] == "107100"
    assert calls == ["Bastion"]
    assert cache.store["gog:1207666643"]["cloud"] == {"gog": True}


@pytest.mark.asyncio
async def test_stamped_entry_is_returned_untouched(_stub_fetch) -> None:
    stamped = dict(_LEGACY_ENTRY)
    stamped[metadata_service._SAVEDATA_SCHEMA_KEY] = (
        metadata_service._SAVEDATA_SCHEMA
    )
    cache = _Cache({"gog:1207666643": stamped})
    calls = _stub_fetch(_FRESH_UNIFIDB)

    out = await _service(cache).enrich(_game())

    assert out is stamped
    assert calls == [], "an already-migrated entry must not re-query the catalog"


@pytest.mark.asyncio
async def test_game_with_no_catalog_save_data_is_stamped_anyway(_stub_fetch) -> None:
    """Otherwise every enrich() re-queries the catalog for that game forever."""
    cache = _Cache({"gog:1207666643": dict(_LEGACY_ENTRY)})
    calls = _stub_fetch({"description": "An action RPG."})

    svc = _service(cache)
    await svc.enrich(_game())
    await svc.enrich(_game())

    assert calls == ["Bastion"], "second enrich must hit the stamp, not the CDN"
    entry = cache.store["gog:1207666643"]
    assert entry[metadata_service._SAVEDATA_SCHEMA_KEY] == (
        metadata_service._SAVEDATA_SCHEMA
    )
    assert "cloud" not in entry


@pytest.mark.asyncio
async def test_catalog_failure_still_serves_the_cached_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CDN outage must degrade to the old data, never raise into the sync."""
    async def boom(_game, config=None):
        raise RuntimeError("cdn down")
    monkeypatch.setattr(metadata_service.metadata_sources, "fetch_unifidb", boom)
    cache = _Cache({"gog:1207666643": dict(_LEGACY_ENTRY)})

    out = await _service(cache).enrich(_game())

    assert out["title"] == "Bastion"
    # Not stamped — so it retries next time rather than burying the gap.
    assert metadata_service._SAVEDATA_SCHEMA_KEY not in out


@pytest.mark.asyncio
async def test_negative_entry_short_circuits_before_topup(_stub_fetch) -> None:
    cache = _Cache({"gog:1207666643": {"_negative": True, "title": "Bastion"}})
    calls = _stub_fetch(_FRESH_UNIFIDB)

    assert await _service(cache).enrich(_game()) == {}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [
    {"_negative": True, "title": "Bastion FitGirl"},
    {"_negative": True},  # written before the title was recorded
])
async def test_negative_entry_for_another_title_is_retried(
    _stub_fetch, monkeypatch, marker,
) -> None:
    """A miss is about the title that was searched; a corrected title
    (an uploader tag stripped, a curated rename) must be asked about."""
    async def no_steam(*_a, **_k):
        return {}
    monkeypatch.setattr(
        metadata_service.metadata_sources, "fetch_steam_store", no_steam,
    )
    cache = _Cache({"gog:1207666643": dict(marker)})
    calls = _stub_fetch(_FRESH_UNIFIDB)

    out = await _service(cache).enrich(_game())

    assert calls == ["Bastion"]
    assert out["description"] == "An action RPG."
    assert "_negative" not in cache.store["gog:1207666643"]


@pytest.mark.asyncio
async def test_a_new_miss_records_the_title_it_searched(
    _stub_fetch, monkeypatch,
) -> None:
    async def no_steam(*_a, **_k):
        return {}
    monkeypatch.setattr(
        metadata_service.metadata_sources, "fetch_steam_store", no_steam,
    )
    cache = _Cache()
    _stub_fetch({})

    assert await _service(cache).enrich(_game()) == {}
    assert cache.store["gog:1207666643"] == {"_negative": True, "title": "Bastion"}


@pytest.mark.asyncio
async def test_topup_fills_empty_description_and_publisher(_stub_fetch) -> None:
    """Both were read under names the catalog never used, so every cached
    entry holds them empty; the top-up fills them without re-fetching Steam."""
    entry = {"description": "", "publisher": "", "genres": ["RPG"]}
    cache = _Cache({"gog:1207666643": entry})
    _stub_fetch({"description": "An action RPG.", "publisher": "Supergiant"})

    out = await _service(cache).enrich(_game())

    assert out["description"] == "An action RPG."
    assert out["publisher"] == "Supergiant"
    assert out["genres"] == ["RPG"]


@pytest.mark.asyncio
async def test_topup_never_overwrites_a_filled_description(_stub_fetch) -> None:
    entry = {"description": "Steam copy.", "publisher": "Steam Pub"}
    cache = _Cache({"gog:1207666643": entry})
    _stub_fetch({"description": "Catalog copy.", "publisher": "Catalog Pub"})

    out = await _service(cache).enrich(_game())

    assert out["description"] == "Steam copy."
    assert out["publisher"] == "Steam Pub"
