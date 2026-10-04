"""The Xbox purchase index: persistence, Steam matching, and when it refreshes.

The ribbon must never lose ownership to a Microsoft hiccup, and must never
keep showing purchases after a sign-out. Pinned here:

- a failed read, and an empty answer after a non-empty list, keep the last
  good index (one key, replaced whole);
- sign-out clears it; sign-in forces a fresh read;
- the refresh waits for the sync's last storesearch user (``proton_meta``)
  and uses the games the ``artwork`` phase carried;
- Steam matches come from the library's own mapping first, then from
  search; a miss is retried after a week or when the title changes, and a
  positive match survives a retitle.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifideck.core.steam_appid_map import STEAM_REAL_APPID_NS
from unifideck.core.types import Events
from unifideck.core.types.domain import Game
from unifideck.services.microsoft_ownership import index as idx
from unifideck.services.microsoft_ownership import service as svc_module
from unifideck.services.microsoft_ownership import steam_resolve
from unifideck.services.microsoft_ownership.service import MicrosoftOwnershipService
from unifideck.stores.microsoft.ownership import OwnedFetch, OwnedProduct


class _Cache:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get(self, cache: str, key: str) -> Any:
        return self.data.get((cache, key))

    def set(self, cache: str, key: str, value: Any, *, flush: bool = True) -> None:
        self.data[(cache, key)] = value

    def delete(self, cache: str, key: str) -> None:
        self.data.pop((cache, key), None)


class _Bus:
    def subscribe(self, *a: Any, **k: Any) -> Any:
        return lambda: None

    async def emit(self, *a: Any, **k: Any) -> None:
        pass


class _Store:
    def __init__(self, *fetches: OwnedFetch) -> None:
        self.fetches = list(fetches)
        self.calls = 0

    async def get_owned_products(self) -> OwnedFetch:
        self.calls += 1
        return self.fetches.pop(0) if len(self.fetches) > 1 else self.fetches[0]


class _Registry:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def get(self, store_id: str) -> _Store:
        return self.store


def _owned(**titles: str) -> OwnedFetch:
    return OwnedFetch(ok=True, products={
        pid: OwnedProduct(title, pc=False, xbox=True, xpa=False, gold=False, source="collections")
        for pid, title in titles.items()
    }, stats={"owned": len(titles)})


@pytest.fixture(autouse=True)
def _no_steam(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    searched: list[str] = []

    async def fake_search(title: str, config: Any = None, session: Any = None) -> Any:
        searched.append(title)
        return {"app_id": 814380} if title.startswith("Sekiro") else None

    monkeypatch.setattr(steam_resolve, "search_store", fake_search)
    monkeypatch.setattr(steam_resolve, "_SEARCH_PAUSE_SECONDS", 0)
    return searched


def _service(store: _Store, cache: _Cache | None = None) -> tuple[MicrosoftOwnershipService, _Cache]:
    cache = cache or _Cache()
    return MicrosoftOwnershipService(_Bus(), _Registry(store), cache), cache  # type: ignore[arg-type]


async def _refresh(service: MicrosoftOwnershipService) -> None:
    await service._on_auth_complete(store="microsoft")
    assert service._task is not None
    await service._task


async def test_first_read_builds_the_view_and_maps_to_steam(_no_steam: list[str]) -> None:
    service, cache = _service(_Store(_owned(BQD5WRRP2D6Q="Sekiro: Shadows Die Twice")))
    await _refresh(service)
    view = service.purchase_indexes()["microsoft"]
    assert view.by_steam_appid == {814380: ("BQD5WRRP2D6Q",)}
    assert idx.load_index(cache) is not None


async def test_a_failed_read_keeps_the_last_good_list() -> None:
    store = _Store(_owned(A="Sekiro"), OwnedFetch.failed("collections.mp.microsoft.com answered 503"))
    service, cache = _service(store)
    await _refresh(service)
    await _refresh(service)
    assert list(idx.load_index(cache)["products"]) == ["A"]
    assert "microsoft" in service.purchase_indexes()


async def test_an_empty_answer_after_a_list_is_not_trusted() -> None:
    service, cache = _service(_Store(_owned(A="Sekiro"), _owned()))
    await _refresh(service)
    await _refresh(service)
    assert list(idx.load_index(cache)["products"]) == ["A"]


async def test_sign_out_forgets_the_purchases() -> None:
    service, cache = _service(_Store(_owned(A="Sekiro")))
    await _refresh(service)
    await service._on_logout(store="microsoft")
    assert service.purchase_indexes() == {}
    assert idx.load_index(cache) is None


async def test_other_stores_events_are_ignored() -> None:
    store = _Store(_owned(A="Sekiro"))
    service, _ = _service(store)
    await service._on_auth_complete(store="gog")
    await service._on_logout(store="gog")
    assert store.calls == 0


async def test_refresh_waits_for_proton_meta_and_uses_artwork_games(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(_owned(PID1="Forza Horizon 4"))
    service, cache = _service(store)
    cache.set(STEAM_REAL_APPID_NS, "-11", 1293830)
    row = Game(app_id=-11, store="microsoft", store_game_id="pid1", title="Forza Horizon 4")
    await service._on_post_sync_phase(phase="artwork", active=False, sync_kwargs={"games": [row]})
    assert store.calls == 0
    await service._on_post_sync_phase(phase="proton_meta", active=True)
    assert service._task is None
    await service._on_post_sync_phase(phase="proton_meta", active=False)
    assert service._task is not None
    await service._task
    assert service.purchase_indexes()["microsoft"].by_steam_appid == {1293830: ("PID1",)}


async def test_a_fresh_list_is_not_reread_after_every_sync() -> None:
    store = _Store(_owned(A="Sekiro"))
    service, _ = _service(store)
    await _refresh(service)
    await service._on_post_sync_phase(phase="proton_meta", active=False)
    assert service._task is not None
    await service._task
    assert store.calls == 1


async def test_nudge_only_refreshes_a_stale_list(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(_owned(A="Sekiro"))
    service, _ = _service(store)
    await _refresh(service)
    service.nudge()
    assert not service._busy()
    service._index["fetched_at"] = 0  # type: ignore[index]
    service.nudge()
    assert service._busy()
    await service._task  # type: ignore[misc]
    assert store.calls == 2


async def test_failed_read_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(OwnedFetch.failed("offline"))
    service, _ = _service(store)
    await _refresh(service)
    service.nudge()  # within the retry window
    assert not service._busy()


async def test_stop_cancels_a_refresh_in_flight() -> None:
    gate = asyncio.Event()

    class _SlowStore(_Store):
        async def get_owned_products(self) -> OwnedFetch:
            await gate.wait()
            return _owned()

    service, _ = _service(_SlowStore(_owned()))
    await service._on_auth_complete(store="microsoft")
    task = service._task
    await service.stop()
    assert task is not None and task.cancelled()


# ── the index itself ─────────────────────────────────────────────────

def test_merge_keeps_matches_and_resets_stale_misses() -> None:
    prev = {"version": 1, "fetched_at": 1, "products": {
        "A": {"title": "Old title", "steam_appid": 42, "steam_checked_at": 5},
        "B": {"title": "Was missing", "steam_appid": idx.STEAM_MISS, "steam_checked_at": 5},
        "C": {"title": "Same", "steam_appid": idx.STEAM_MISS, "steam_checked_at": 5},
    }}
    new = idx.merge(prev, _owned(A="New title", B="Renamed", C="Same"), now=10)
    assert new["products"]["A"]["steam_appid"] == 42          # match survives a retitle
    assert new["products"]["B"]["steam_appid"] == idx.STEAM_PENDING  # new title, new search
    assert new["products"]["C"]["steam_appid"] == idx.STEAM_MISS


def test_misses_are_retried_after_a_week() -> None:
    entry = {"title": "X", "steam_appid": idx.STEAM_MISS, "steam_checked_at": 0,
             "steam_search_rev": idx.STEAM_SEARCH_REVISION}
    assert idx.needs_steam_search(entry, now=idx.MISS_RETRY_SECONDS + 1)
    assert not idx.needs_steam_search(entry, now=10)
    assert not idx.needs_steam_search({"title": "", "steam_appid": 0})


def test_a_miss_from_an_older_search_is_retried_at_once() -> None:
    """Revision 2 strips "(PC)": "DOOM Eternal Standard Edition (PC)" was a
    miss under 1 and must not wait a week for the better search."""
    old = {"title": "DOOM Eternal Standard Edition (PC)", "steam_appid": idx.STEAM_MISS,
           "steam_checked_at": 5}  # written before revisions existed
    assert idx.needs_steam_search(old, now=10)
    merged = idx.merge({"products": {"A": old}}, _owned(A=old["title"]), now=10)
    assert idx.needs_steam_search(merged["products"]["A"], now=10)
    assert not idx.needs_steam_search(
        {"title": "Mapped", "steam_appid": 42, "steam_search_rev": 1}, now=10,
    )


async def test_a_search_records_its_revision(_no_steam: list[str]) -> None:
    index = idx.merge(None, _owned(P="Not on Steam"), now=1)
    await steam_resolve.resolve_pending(index, None, save=lambda: None)
    entry = index["products"]["P"]
    assert entry["steam_appid"] == idx.STEAM_MISS
    assert entry["steam_search_rev"] == idx.STEAM_SEARCH_REVISION
    assert not idx.needs_steam_search(entry, now=entry["steam_checked_at"] + 10)


def test_an_index_in_an_unknown_shape_is_ignored() -> None:
    cache = _Cache()
    cache.set(idx.CACHE_NAME, "index", {"version": 99, "products": {}})
    assert idx.load_index(cache) is None


async def test_steam_search_budget(monkeypatch: pytest.MonkeyPatch, _no_steam: list[str]) -> None:
    index = idx.merge(None, _owned(**{f"P{i}": f"Game {i}" for i in range(5)}), now=1)
    saves: list[int] = []
    matched, missed = await steam_resolve.resolve_pending(index, None, save=lambda: saves.append(1), budget=3)
    assert (matched, missed) == (0, 3)
    assert len(_no_steam) == 3


def test_the_cache_namespace_never_expires() -> None:
    from unifideck.bootstrap.cache_registry import _NAMED_CACHES
    assert dict(_NAMED_CACHES)["microsoft_owned"] == 0


def test_the_service_reads_the_events_it_subscribes_to() -> None:
    handlers = {
        name for name in dir(MicrosoftOwnershipService) if name.startswith("_on_")
    }
    assert handlers == {"_on_post_sync_phase", "_on_auth_complete", "_on_logout"}
    assert svc_module.Events is Events
