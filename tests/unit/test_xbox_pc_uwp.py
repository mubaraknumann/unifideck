"""UWP-only Store titles are listed as "not supported" with reason ``uwp``.

Xodus installs MSIXVC packages only. A title whose desktop package is a UWP
format (Appx, EAppxBundle, ...) used to vanish from the list; it now gets a
row the user can see, and its install is refused with its own code.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.stores.microsoft.pc_library.entitlements import (
    Entitlement,
    EntitlementSnapshot,
)
from unifideck.stores.microsoft.pc_library.package_plan import PlanProducts, build_plan
from unifideck.stores.microsoft.pc_library.rows import UWP_REASON, classify, summarize_roots
from unifideck.stores.microsoft.xodus.installer import plan_code

PID = "9PNJXVCVWD4K"
CID = "11111111-1111-4111-8111-111111111111"
CID2 = "22222222-2222-4222-8222-222222222222"
NOW = 1_800_000_000.0


def _product(*packages: dict[str, Any], kind: str = "Game") -> dict[str, Any]:
    return {"ProductId": PID, "ProductType": kind,
            "LocalizedProperties": [{"ProductTitle": "Racing Game"}],
            "DisplaySkuAvailabilities": [{"Sku": {"Properties": {"Packages": list(packages)}}}]}


def _package(fmt: str, cid: str = CID, platform: str = "Windows.Desktop") -> dict[str, Any]:
    return {"ContentId": cid, "PackageFormat": fmt,
            "PlatformDependencies": [{"PlatformName": platform}]}


def _snapshot() -> EntitlementSnapshot:
    row = Entitlement(product_id=PID, status="Active", is_trial=False, special=False,
                      sharing_source="None", end_date=NOW + 3600, end_date_text="", parent=None)
    return EntitlementSnapshot(account_hash="h", market="US", checked_at=NOW,
                               entries={PID: row}, skipped=0)


def _titles(product: dict[str, Any]) -> list[Any]:
    summaries, unknown_roots = summarize_roots([PID], PlanProducts(products={PID: product}))
    titles, _ = classify(_snapshot(), summaries, unknown_roots, NOW)
    return titles


@pytest.mark.parametrize("fmt", ["EAppxBundle", "AppxBundle", "APPX", "EMsix"])
def test_a_uwp_only_title_is_listed_as_not_supported(fmt: str) -> None:
    assert build_plan(PID, {PID: _product(_package(fmt))}).uwp_only
    titles = _titles(_product(_package(fmt)))
    assert [(t.entitlement_id, t.state, t.reason, t.access) for t in titles] == [
        (PID, "not_supported", UWP_REASON, "owned"),
    ]


def test_an_msixvc_package_beside_a_uwp_one_stays_installable() -> None:
    product = _product(_package("EAppxBundle"), _package("MSIXVC", cid=CID2))
    assert not build_plan(PID, {PID: product}).uwp_only
    [title] = _titles(product)
    assert title.state == "ready" and title.content_id == CID2


@pytest.mark.parametrize("product", [
    _product(),  # the catalog lists no package at all: nothing says it is UWP
    _product(_package("EAppx", platform="Xbox.One")),  # a console package only
    _product(_package("XVC")),  # not a UWP format
])
def test_other_unavailable_titles_are_still_dropped(product: dict[str, Any]) -> None:
    assert _titles(product) == []


def test_a_uwp_add_on_is_not_called_a_uwp_game() -> None:
    plan = build_plan(PID, {PID: _product(_package("EAppx"), kind="Durable")})
    assert not plan.uwp_only


def test_install_of_a_uwp_title_is_refused_with_its_own_code() -> None:
    assert plan_code({"state": "not_supported", "reason": UWP_REASON}) == "xbox_pc_unsupported_package"
    assert plan_code({"state": "not_supported", "reason": "ea_play"}) == "xbox_pc_unsupported_plan"
