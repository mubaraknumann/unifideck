"""Where an owned Microsoft product plays: PC, Xbox, or both (Play Anywhere).

Read from the public displaycatalog (``microsoft_catalog.fetch_products``),
the same data the Xbox app's "playable on PC" filter showed for a real
account on 2026-10-02:

- packages carry ``PlatformDependencies[].PlatformName``:
  ``Windows.Desktop`` / ``Windows.Universal`` is PC, ``Windows.Xbox`` is
  console; ``Properties.XboxConsoleGenCompatible`` is console too;
- Xbox Play Anywhere is the ``XboxXPA`` sku flag or the ``XPA`` attribute;
- a bundle ("DOOM Eternal Standard Edition (PC)") has no packages of its
  own: its platforms are its parts' (``BundledSkus[].BigId``), and failing
  that its title says "(PC)" or "for Xbox".

Collections calls some add-ons ``Game`` too (an artbook, an upgrade, a
friend's pass), and so does the catalog. Matched to Steam by title, one
could claim ownership of its base game, so :func:`looks_like_extra` keeps
them out of the ownership index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from unifideck.stores.microsoft.microsoft_catalog import fetch_products, product_title

_PC_PLATFORMS = frozenset({"Windows.Desktop", "Windows.Universal"})
_CONSOLE_PLATFORM = "Windows.Xbox"
_PC_TITLE = re.compile(r"\((?:PC|Windows)\)|[-\u2013]\s*Windows\b|\bfor Windows\b", re.I)
_XBOX_TITLE = re.compile(r"\bfor Xbox\b|\bXbox (?:One|Series)\b", re.I)
_EXTRA_TITLE = re.compile(
    r"\b(?:add-?on|upgrade|soundtrack|artbook|season pass|expansion pass"
    r"|friend'?s pass|dlc|demo)\b",
    re.I,
)


@dataclass(frozen=True)
class ProductInfo:
    """What the catalog says about one product."""

    title: str
    pc: bool = False
    xbox: bool = False
    xpa: bool = False
    demo: bool = False

    @property
    def has_platform(self) -> bool:
        """True when the catalog named at least one platform."""
        return self.pc or self.xbox


def looks_like_extra(title: str) -> bool:
    """An add-on that the store files as a game (artbook, upgrade, pass…)."""
    return bool(_EXTRA_TITLE.search(title))


async def describe_products(
    product_ids: list[str], market: str = "US",
) -> dict[str, ProductInfo]:
    """Title and platforms per UPPER-case product id; unknown ids are absent."""
    raw = await fetch_products(product_ids, market)
    info = {pid: _describe(product) for pid, product in raw.items()}
    bundles = {
        pid: _bundled_ids(raw[pid]) for pid, i in info.items() if not i.has_platform
    }
    wanted = sorted({b for parts in bundles.values() for b in parts} - raw.keys())
    if wanted:
        raw.update(await fetch_products(wanted, market))
    for pid, parts in bundles.items():
        info[pid] = _with_parts(info[pid], [_describe(raw[p]) for p in parts if p in raw])
    return info


def _describe(product: dict[str, Any]) -> ProductInfo:
    raw_props = product.get("Properties")
    props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    platforms, xpa = _sku_facts(product)
    attrs = {a.get("Name") for a in props.get("Attributes") or () if isinstance(a, dict)}
    return ProductInfo(
        title=product_title(product),
        pc=bool(platforms & _PC_PLATFORMS),
        xbox=_CONSOLE_PLATFORM in platforms or bool(props.get("XboxConsoleGenCompatible")),
        xpa=xpa or "XPA" in attrs,
        demo=props.get("IsDemo") is True,
    )


def _skus(product: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for availability in product.get("DisplaySkuAvailabilities") or ():
        sku = availability.get("Sku") if isinstance(availability, dict) else None
        props = sku.get("Properties") if isinstance(sku, dict) else None
        if isinstance(props, dict):
            out.append(props)
    return out


def _sku_facts(product: dict[str, Any]) -> tuple[set[str], bool]:
    """Every package platform named by the product's skus, and the XPA flag."""
    platforms: set[str] = set()
    xpa = False
    for sku in _skus(product):
        xpa = xpa or sku.get("XboxXPA") is True
        for package in sku.get("Packages") or ():
            platforms.update(_package_platforms(package))
    return platforms, xpa


def _package_platforms(package: Any) -> set[str]:
    if not isinstance(package, dict):
        return set()
    deps = package.get("PlatformDependencies") or ()
    return {
        d["PlatformName"] for d in deps
        if isinstance(d, dict) and isinstance(d.get("PlatformName"), str)
    }


def _bundled_ids(product: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for sku in _skus(product):
        for part in sku.get("BundledSkus") or ():
            big_id = part.get("BigId") if isinstance(part, dict) else None
            if isinstance(big_id, str) and big_id.upper() not in ids:
                ids.append(big_id.upper())
    return ids


def _with_parts(bundle: ProductInfo, parts: list[ProductInfo]) -> ProductInfo:
    """A bundle's platforms: its parts', else what its title says."""
    pc = any(p.pc for p in parts) or bool(_PC_TITLE.search(bundle.title))
    xbox = any(p.xbox for p in parts) or bool(_XBOX_TITLE.search(bundle.title))
    return ProductInfo(
        title=bundle.title,
        pc=pc,
        xbox=xbox,
        xpa=bundle.xpa or any(p.xpa for p in parts),
        demo=bundle.demo,
    )
