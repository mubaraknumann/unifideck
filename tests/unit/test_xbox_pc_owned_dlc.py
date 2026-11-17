"""``OwnedDlc.for_game``: owned DLC of one game, from entitlements and the catalog."""
from __future__ import annotations

from typing import Any

import pytest

from tests.unit.test_xbox_pc_dlc_catalog_rules import package, product, sku
from unifideck.stores.microsoft.pc_library.entitlements import Entitlement, EntitlementSnapshot
from unifideck.stores.microsoft.pc_library.entitlement_cache import installable_ids as _installable_ids
from unifideck.stores.microsoft.pc_library.owned_dlc import PRODUCT_TTL_S, OwnedDlc

GAME = "C2KDNLT2H7DM"
OTHER_GAME = "9NCJSXWZTP88"
DLC_A = "9NB77CW5J1TT"
DLC_B = "9N77PDNB7CWZ"
FOREIGN = "9N5PQG53S4SW"
BUNDLE = "9PFGWDCW7NL4"
MEMBER = "9N1GHC0V27BP"
GP_PLAN = "CFQ7TTC0KGQ8"
NOW = 1_800_000_000.0


def row(pid: str, parent: str | None = None, **over: Any) -> Entitlement:
    fields: dict[str, Any] = {
        "product_id": pid, "status": "Active", "is_trial": False, "special": False,
        "sharing_source": "None", "end_date": NOW + 86400, "end_date_text": "", "parent": parent,
    }
    fields.update(over)
    return Entitlement(**fields)


def snap(*rows: Entitlement) -> EntitlementSnapshot:
    return EntitlementSnapshot(account_hash="h", market="IN", checked_at=NOW,
                               entries={r.product_id: r for r in rows}, skipped=0)


def dlc_product(pid: str, parent: str, title: str, size: int = 16941056) -> dict[str, Any]:
    p = product(sku(package(size=size)), parents=(parent,), pid=pid)
    p["LocalizedProperties"] = [{"ProductTitle": title}]
    return p


def pass_product() -> dict[str, Any]:
    return product(sku(BundledSkus=[{"BigId": MEMBER}]), parents=(GAME,), pid=BUNDLE)


CATALOG = {
    DLC_A: dlc_product(DLC_A, GAME, "Zeta pack"),
    DLC_B: dlc_product(DLC_B, GAME, "Alpha expansion", size=15_000_000_000),
    FOREIGN: dlc_product(FOREIGN, OTHER_GAME, "Another game's DLC"),
    BUNDLE: pass_product(),
    MEMBER: dlc_product(MEMBER, GAME, "Middle member"),
}


class FakeLibrary:
    def __init__(self, entitlements: EntitlementSnapshot | None, kinds: dict[str, str] | None = None) -> None:
        self.snap = entitlements
        self.kinds = kinds or {}
        self.reads: list[tuple[float, bool]] = []

    def title_for(self, store_game_id: str) -> dict[str, Any] | None:
        if store_game_id != "row-1":
            return None
        return {"target_id": GAME, "product_id": "9P4KMR76PLLQ", "market": "IN"}

    def resolve_market(self, market: str | None) -> str:
        return market or "US"

    def known_kinds(self, market: str) -> dict[str, str]:
        return self.kinds

    async def owned_ids(self, market: str | None, *, max_age_s: float, force: bool = False) -> list[str] | None:
        self.reads.append((max_age_s, force))
        return _installable_ids(self.snap, NOW) if self.snap is not None else None


class FakeFetch:
    def __init__(self, failed: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.failed = failed or set()

    async def __call__(self, ids: list[str], market: str, *, languages: str) -> tuple[dict[str, Any], list[str]]:
        self.calls.append(list(ids))
        return ({i: CATALOG[i] for i in ids if i in CATALOG and i not in self.failed},
                [i for i in ids if i in self.failed])


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def owned(fetch: FakeFetch, library: FakeLibrary, clock: Clock | None = None) -> OwnedDlc:
    return OwnedDlc(library, fetch=fetch, clock=clock or Clock())


ALL_OWNED = snap(row(GAME), row(DLC_A), row(DLC_B, parent=GP_PLAN), row(FOREIGN), row(BUNDLE))


async def test_this_games_dlc_are_listed_by_title() -> None:
    found = await owned(FakeFetch(), FakeLibrary(ALL_OWNED)).for_game("row-1")
    assert found is not None
    assert [d.product_id for d in found] == [DLC_B, MEMBER, DLC_A]
    assert [d.large for d in found] == [True, False, False]


async def test_a_pass_grants_its_members_and_is_not_listed_itself() -> None:
    found = await owned(FakeFetch(), FakeLibrary(snap(row(BUNDLE)))).for_game("row-1")
    assert [d.product_id for d in found or []] == [MEMBER]


@pytest.mark.parametrize("bad", [
    {"status": "Expired"}, {"is_trial": True}, {"special": True},
    {"sharing_source": "Family"}, {"end_date": NOW - 1},
])
async def test_dlc_without_install_access_is_left_out(bad: dict[str, Any]) -> None:
    found = await owned(FakeFetch(), FakeLibrary(snap(row(DLC_A, **bad)))).for_game("row-1")
    assert found == []


async def test_products_known_to_be_games_are_never_fetched() -> None:
    fetch = FakeFetch()
    library = FakeLibrary(ALL_OWNED, kinds={GAME: "Game", DLC_A: "Durable"})
    await owned(fetch, library).for_game("row-1")
    assert GAME not in fetch.calls[0] and DLC_A in fetch.calls[0]


async def test_catalog_facts_are_cached_until_force_or_expiry() -> None:
    fetch, clock = FakeFetch(), Clock()
    dlc = owned(fetch, FakeLibrary(ALL_OWNED), clock)
    await dlc.for_game("row-1")
    calls = len(fetch.calls)
    await dlc.for_game("row-1")
    assert len(fetch.calls) == calls
    await dlc.for_game("row-1", force=True)
    assert len(fetch.calls) > calls
    calls = len(fetch.calls)
    clock.t += PRODUCT_TTL_S
    await dlc.for_game("row-1")
    assert len(fetch.calls) > calls


async def test_a_failed_batch_is_left_out_and_asked_again() -> None:
    fetch = FakeFetch(failed={DLC_A})
    dlc = owned(fetch, FakeLibrary(ALL_OWNED))
    found = await dlc.for_game("row-1")
    assert DLC_A not in [d.product_id for d in found or []]
    fetch.failed.clear()
    found = await dlc.for_game("row-1")
    assert DLC_A in [d.product_id for d in found or []]


async def test_force_reaches_the_entitlement_read() -> None:
    library = FakeLibrary(ALL_OWNED)
    await owned(FakeFetch(), library).for_game("row-1", force=True)
    assert library.reads == [(300.0, True)]


async def test_unknown_game_or_unreadable_entitlements_is_none() -> None:
    assert await owned(FakeFetch(), FakeLibrary(ALL_OWNED)).for_game("nope") is None
    assert await owned(FakeFetch(), FakeLibrary(None)).for_game("row-1") is None
