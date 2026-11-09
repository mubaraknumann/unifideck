"""Tests for ``stores/microsoft/pc_library/package_plan``.

The first six tests are the Rust tests of ``package_plan.rs`` (our build's
``xodus-package-selection`` patch), ported one to one, so the Python port
keeps the backend's verdicts.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from unifideck.stores.microsoft.pc_library.package_plan import (
    CATALOG_LANGUAGES,
    MissingProductError,
    PlanError,
    build_plan,
    fetch_plan_products,
    inspect,
)

ROOT = "9AAAAAAAAAAA"
CHILD = "9BBBBBBBBBBB"
CID = "11111111-1111-4111-8111-111111111111"
CID2 = "22222222-2222-4222-8222-222222222222"


def package(pid: str, platform: str, fmt: str, **pkg: Any) -> dict[str, Any]:
    return {"ProductId": pid, "ProductType": "Game",
            "LocalizedProperties": [{"ProductTitle": "Test game"}],
            "DisplaySkuAvailabilities": [{"Sku": {"Properties": {"Packages": [
                {"ContentId": CID, "PackageFormat": fmt,
                 "PlatformDependencies": [{"PlatformName": platform}], **pkg}]}}}]}


def bundle(pid: str, children: list[str]) -> dict[str, Any]:
    return {"ProductId": pid, "DisplaySkuAvailabilities": [
        {"Sku": {"Properties": {"BundledSkus": [{"BigId": c} for c in children]}}}]}


def _props(product: dict[str, Any], i: int = 0) -> dict[str, Any]:
    return product["DisplaySkuAvailabilities"][i]["Sku"]["Properties"]


def test_direct_and_selected_membership() -> None:
    plan = build_plan(ROOT, {ROOT: package(ROOT, "Windows.Desktop", "MSIXVC")})
    auto = plan.automatic()
    assert auto is not None and auto.content_id == CID
    assert plan.status == "ready"


@pytest.mark.parametrize(("platform", "fmt"), [("Xbox.One", "MSIXVC"), ("Windows.Desktop", "APPX")])
def test_unsupported_is_not_guessed(platform: str, fmt: str) -> None:
    plan = build_plan(ROOT, {ROOT: package(ROOT, platform, fmt)})
    assert plan.automatic() is None and plan.supported() == []
    assert plan.status == "unavailable"


def test_ambiguous_bundle_is_a_choice() -> None:
    second = package(CHILD, "Windows.Desktop", "MSIXVC")
    _props(second)["Packages"][0]["ContentId"] = CID2
    top = "9CCCCCCCCCCC"
    plan = build_plan(top, {top: bundle(top, [ROOT, CHILD]),
                            ROOT: package(ROOT, "Windows.Desktop", "MSIXVC"), CHILD: second})
    assert plan.automatic() is None
    assert {c.content_id for c in plan.supported()} == {CID, CID2}
    assert plan.status == "choice"


def test_cycles_missing_children_and_identity_fail_closed() -> None:
    with pytest.raises(PlanError):
        build_plan(ROOT, {ROOT: bundle(ROOT, [ROOT])})
    with pytest.raises(MissingProductError) as missing:
        build_plan(ROOT, {ROOT: bundle(ROOT, [CHILD])})
    assert missing.value.product_id == CHILD
    with pytest.raises(PlanError, match="identity"):
        build_plan(ROOT, {ROOT: package(CHILD, "Windows.Desktop", "MSIXVC")})


def test_shared_children_deduplicate_and_addons_need_explicit_selection() -> None:
    child = package(CHILD, "Windows.Desktop", "MSIXVC")
    child["ProductType"] = "GameAddOn"
    plan = build_plan(ROOT, {ROOT: bundle(ROOT, [CHILD, CHILD]), CHILD: child})
    assert len(plan.choices) == 1
    assert plan.automatic() is None and plan.status == "choice"


def test_retail_preorder_and_trial_sku_order_never_hides_retail_package() -> None:
    product = package(ROOT, "Windows.Desktop", "MSIXVC")
    retail = product["DisplaySkuAvailabilities"][0]
    trial = copy.deepcopy(retail)
    trial["Sku"]["Properties"]["IsTrial"] = True
    preorder = copy.deepcopy(retail)
    preorder["Sku"]["Properties"]["IsPreOrder"] = True
    for skus in ([preorder, retail, trial], [trial, preorder, retail]):
        product["DisplaySkuAvailabilities"] = copy.deepcopy(skus)
        assert build_plan(ROOT, {ROOT: product}).automatic() is not None
    product["DisplaySkuAvailabilities"] = [trial, preorder]
    assert build_plan(ROOT, {ROOT: product}).automatic() is None


def test_console_root_can_expose_declared_pc_child() -> None:
    root = package(ROOT, "Xbox.One", "XVC")
    _props(root)["Packages"][0]["ContentId"] = CID2
    _props(root)["BundledSkus"] = [{"BigId": CHILD}]
    plan = build_plan(ROOT, {ROOT: root, CHILD: package(CHILD, "Windows.Desktop", "MSIXVC")})
    assert len(plan.choices) == 2
    auto = plan.automatic()
    assert auto is not None and auto.product_id == CHILD


# ── Beyond the Rust tests ────────────────────────────────────────────────

def test_size_and_title_cleanup() -> None:
    product = package(ROOT, "Windows.Desktop", "msixvc", MaxDownloadSizeInBytes=123456)
    product["LocalizedProperties"][0]["ProductTitle"] = "Bal\x00atro\n" + "x" * 300
    choices, children = inspect(product, ROOT)
    assert children == []
    assert choices[0].size_bytes == 123456
    assert choices[0].format == "MSIXVC" and choices[0].supported
    assert choices[0].title.startswith("Balatro") and len(choices[0].title) == 240


def test_licensing_keys_are_followed_only_without_bundled_skus() -> None:
    product = {"ProductId": ROOT, "DisplaySkuAvailabilities": [{
        "Sku": {"Properties": {}},
        "Availabilities": [{"LicensingData": {"SatisfyingEntitlementKeys": [{"EntitlementKeys": [
            f"big:{CHILD}:0010", f"big:{ROOT}:0010", "big:CFQ7TTC0KGQ8:0001", "wes:App:1",
        ]}]}}],
    }]}
    assert inspect(product, ROOT)[1] == [CHILD]
    _props(product)["BundledSkus"] = [{"BigId": "9DDDDDDDDDDD"}]
    assert inspect(product, ROOT)[1] == ["9DDDDDDDDDDD"]


def test_a_bad_content_id_is_ignored() -> None:
    product = package(ROOT, "Windows.Desktop", "MSIXVC")
    _props(product)["Packages"][0]["ContentId"] = "not-a-uuid"
    assert inspect(product, ROOT)[0] == []


def test_too_deep_fails() -> None:
    ids = [f"9{i:011d}" for i in range(9)]
    products = {pid: bundle(pid, [ids[i + 1]]) for i, pid in enumerate(ids[:-1])}
    products[ids[-1]] = package(ids[-1], "Windows.Desktop", "MSIXVC")
    with pytest.raises(PlanError, match="deep"):
        build_plan(ids[0], products)


# ── fetch_plan_products ──────────────────────────────────────────────────

class _Catalog:
    def __init__(self, products: dict[str, dict[str, Any]], failing: set[str] | None = None,
                 delay: float = 0.0) -> None:
        self.products = products
        self.failing = failing or set()
        self.delay = delay
        self.calls: list[list[str]] = []

    async def __call__(self, ids: list[str], market: str, *, languages: str) -> Any:
        assert languages == CATALOG_LANGUAGES and market == "IN"
        self.calls.append(list(ids))
        if self.delay:
            await asyncio.sleep(self.delay)
        found = {i: self.products[i] for i in ids if i in self.products and i not in self.failing}
        return found, [i for i in ids if i in self.failing]


async def test_fetch_walks_children_level_by_level() -> None:
    catalog = _Catalog({ROOT: bundle(ROOT, [CHILD]), CHILD: package(CHILD, "Windows.Desktop", "MSIXVC")})
    found = await fetch_plan_products([ROOT, "9ZZZZZZZZZZZ"], "IN", fetch=catalog, deadline=1e18)
    assert catalog.calls == [[ROOT, "9ZZZZZZZZZZZ"], [CHILD]]
    assert set(found.products) == {ROOT, CHILD}
    assert found.absent == {"9ZZZZZZZZZZZ"} and found.unknown == set()


async def test_a_failed_batch_is_unknown_not_absent() -> None:
    catalog = _Catalog({ROOT: bundle(ROOT, [CHILD])}, failing={CHILD})
    found = await fetch_plan_products([ROOT], "IN", fetch=catalog, deadline=1e18)
    assert found.unknown == {CHILD} and found.absent == set()


async def test_the_deadline_makes_the_rest_unknown() -> None:
    catalog = _Catalog({ROOT: package(ROOT, "Windows.Desktop", "MSIXVC")}, delay=5.0)
    loop = asyncio.get_running_loop()
    found = await fetch_plan_products([ROOT], "IN", fetch=catalog, deadline=loop.time() + 0.05,
                                      clock=loop.time)
    assert found.unknown == {ROOT} and found.products == {}
    spent = await fetch_plan_products([ROOT], "IN", fetch=catalog, deadline=0.0, clock=lambda: 1.0)
    assert spent.unknown == {ROOT}
