"""``pc_library.dlc``: which game a DLC belongs to, from its catalog record.

The product shape follows real displaycatalog records (2026-11-17): a
``Durable`` whose ``MarketProperties`` name one ``addOnParent``, with PC
and console packages in a retail SKU and the same packages in a pre-order
SKU.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from unifideck.stores.microsoft.pc_library.dlc import (
    LARGE_DLC_BYTES,
    DlcInfo,
    addon_parent,
    bundle_members,
    dlc_of,
)

DLC = "9NB77CW5J1TT"
PARENT = "C2KDNLT2H7DM"
CID = "bfedb5f5-cd3c-40b7-9b43-f169b55cbc78"
FAMILY = "Example.Base_8wekyb3d8bbwe"


def package(fmt: str = "MSIXVC", platform: str = "Windows.Desktop", size: int = 16941056,
            **over: Any) -> dict[str, Any]:
    pkg = {
        "ContentId": CID, "PackageFormat": fmt, "Architectures": ["x64"],
        "MainPackageFamilyNameForDlc": FAMILY, "MaxDownloadSizeInBytes": size,
        "PlatformDependencies": [{"PlatformName": platform}],
    }
    pkg.update(over)
    return pkg


def sku(*packages: dict[str, Any], **props: Any) -> dict[str, Any]:
    return {"Sku": {"Properties": {"IsTrial": False, "IsPreOrder": False,
                                   "Packages": list(packages), **props}}}


def product(*skus: dict[str, Any], kind: str = "Durable", parents: tuple[str, ...] = (PARENT,),
            pid: str = DLC) -> dict[str, Any]:
    related = [{"RelationshipType": "addOnParent", "RelatedProductId": p} for p in parents]
    related.append({"RelationshipType": "SellableBy", "RelatedProductId": PARENT})
    return {
        "ProductId": pid, "ProductType": kind,
        "LocalizedProperties": [{"ProductTitle": "Example add-on"}],
        "MarketProperties": [{"RelatedProducts": related}],
        "DisplaySkuAvailabilities": list(skus or (sku(package("XVC", "Windows.Xbox"), package()),)),
    }


def test_a_pc_durable_is_a_dlc_of_its_parent() -> None:
    info = dlc_of(product(), DLC)
    assert info == DlcInfo(product_id=DLC, parent=PARENT, title="Example add-on",
                           size_bytes=16941056)
    assert info is not None and info.large is False


def test_a_preorder_sku_beside_a_retail_one_does_not_matter() -> None:
    both = product(sku(package()), sku(package(), IsPreOrder=True))
    assert dlc_of(both, DLC) is not None


@pytest.mark.parametrize("parents", [(), (PARENT, "9CCCCCCCCCCC"), ("bad-id",), (DLC,)])
def test_no_single_valid_parent_is_not_a_dlc(parents: tuple[str, ...]) -> None:
    assert addon_parent(product(parents=parents)) is None
    assert dlc_of(product(parents=parents), DLC) is None


def test_the_same_parent_in_two_markets_is_one_parent() -> None:
    p = product()
    p["MarketProperties"].append(copy.deepcopy(p["MarketProperties"][0]))
    assert addon_parent(p) == PARENT


@pytest.mark.parametrize("pkg", [
    package("XVC"),
    package(platform="Windows.Xbox"),
    package(Architectures=["arm64"]),
    package(MainPackageFamilyNameForDlc="../outside"),
    package(MainPackageFamilyNameForDlc=None),
    package(ContentId="not-a-uuid"),
])
def test_no_installable_pc_package_is_not_a_dlc(pkg: dict[str, Any]) -> None:
    assert dlc_of(product(sku(pkg)), DLC) is None


@pytest.mark.parametrize("props", [{"IsTrial": True}, {"IsPreOrder": True}])
def test_trial_or_preorder_only_is_not_a_dlc(props: dict[str, Any]) -> None:
    assert dlc_of(product(sku(package(), **props)), DLC) is None


def test_a_game_is_not_a_dlc() -> None:
    assert dlc_of(product(kind="Game"), DLC) is None


def test_another_products_record_is_refused() -> None:
    assert dlc_of(product(), PARENT) is None


def test_size_splits_small_from_large() -> None:
    big = dlc_of(product(sku(package(size=LARGE_DLC_BYTES + 1))), DLC)
    edge = dlc_of(product(sku(package(size=LARGE_DLC_BYTES))), DLC)
    unknown = dlc_of(product(sku(package(MaxDownloadSizeInBytes=None))), DLC)
    assert big is not None and big.large
    assert edge is not None and not edge.large
    assert unknown is not None and unknown.large  # never downloaded unasked


def test_dict_round_trip() -> None:
    info = dlc_of(product(), DLC)
    assert info is not None
    assert DlcInfo.from_dict(info.to_dict()) == info
    assert DlcInfo.from_dict({"product_id": "bad", "parent": PARENT}) is None


# ── passes ──────────────────────────────────────────────────────────────
MEMBERS = ["9N1GHC0V27BP", "9N23P6QNFNMX"]
BUNDLE = "9PFGWDCW7NL4"


def a_pass(**props: Any) -> dict[str, Any]:
    bundled = [{"BigId": m} for m in reversed(MEMBERS)] + [{"BigId": BUNDLE}]
    return product(sku(BundledSkus=bundled, **props), pid=BUNDLE)


def test_a_pass_lists_its_members() -> None:
    assert bundle_members(a_pass(), BUNDLE) == MEMBERS
    assert dlc_of(a_pass(), BUNDLE) is None


def test_a_dlc_with_its_own_package_is_not_a_pass() -> None:
    assert bundle_members(product(), DLC) == []


def test_a_game_bundle_is_not_a_dlc_pass() -> None:
    game = a_pass()
    game["ProductType"] = "Game"
    assert bundle_members(game, BUNDLE) == []
