"""Which game a DLC belongs to, and whether Xodus can install it: from the catalog.

A port of the ``dlc_parent`` and Durable rules of our backend's
``xodus-durable-content`` patch (PATCHES.md backend row 43), so the plugin
can tell, from one batched displaycatalog read, which owned add-ons belong
to a game without running ``resolve-package`` once per add-on.

A product is an installable DLC when:

- ``ProductType`` is ``Durable``;
- ``MarketProperties[].RelatedProducts[]`` names exactly one
  ``addOnParent``, a valid product id other than itself;
- one of its SKUs, neither a trial nor a pre-order, has a
  ``Windows.Desktop`` ``MSIXVC`` package for ``x64`` with a valid
  ``MainPackageFamilyNameForDlc``.

A pass or bundle of add-ons (a ``Durable`` with no package of its own,
such as an expansion pass) is not itself installable: its
``BundledSkus[].BigId`` members are (:func:`bundle_members`). The account
may own only the pass, so its members are listed as owned through it,
the same expansion the backend's ``bundle-membership`` patch does.

Install still runs ``resolve-package --include-dlc`` and checks the parent
again (``package_choice.select_dlc_content``): this decides what is
listed, the CLI decides what is downloaded.

The catalog cannot tell content from cosmetics (a 10 MB license file can
unlock an expansion, and a 130 GB pack can be videos only), so the size is
the only thing used to split DLC: ``large`` ones are offered in a picker
before install, the rest download with the game.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from .entitlements import is_product_id
from .package_plan import _bundled_ids, _list, _size, _sku_properties, _text

#: DLC above this many bytes is offered in the install picker instead of
#: downloading with the game. A DLC with no size counts as large.
LARGE_DLC_BYTES = 1024 ** 3

_FAMILY_NAME = re.compile(r"^[A-Za-z0-9.\-]+_[A-Za-z0-9]+\Z")
_FAMILY_MAX = 200


@dataclass(frozen=True)
class DlcInfo:
    """One installable DLC as the catalog describes it."""

    product_id: str
    parent: str
    title: str
    size_bytes: int | None

    @property
    def large(self) -> bool:
        """Offered in the picker rather than downloaded with the game."""
        return self.size_bytes is None or self.size_bytes > LARGE_DLC_BYTES

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape the RPC and the cache use."""
        return {
            "product_id": self.product_id, "parent": self.parent, "title": self.title,
            "size_bytes": self.size_bytes, "large": self.large,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DlcInfo | None:
        """The DLC a cached dict describes, or None when it is invalid."""
        if not isinstance(raw, dict):
            return None
        pid, parent = raw.get("product_id"), raw.get("parent")
        if not is_product_id(pid) or not is_product_id(parent):
            return None
        size = raw.get("size_bytes")
        return cls(
            product_id=pid, parent=parent, title=str(raw.get("title") or pid),
            size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
        )


def addon_parent(product: dict[str, Any]) -> str | None:
    """The one ``addOnParent`` of ``product``, or None (none, several, or bad)."""
    own = product.get("ProductId")
    parents: set[str] = set()
    for market in _list(product, "MarketProperties"):
        for relation in _list(market, "RelatedProducts"):
            if not isinstance(relation, dict) or relation.get("RelationshipType") != "addOnParent":
                continue
            related = relation.get("RelatedProductId")
            if not is_product_id(related) or related == own:
                return None
            parents.add(related)
    return parents.pop() if len(parents) == 1 else None


def _family_name(value: Any) -> bool:
    """A package family name (``Name_publisherhash``), as the backend checks it."""
    return isinstance(value, str) and len(value) <= _FAMILY_MAX and _FAMILY_NAME.match(value) is not None


def _pc_package(package: Any) -> bool:
    """A ``Windows.Desktop`` MSIXVC x64 DLC package with its base family named."""
    if not isinstance(package, dict):
        return False
    platforms = {_text(dep, "PlatformName") for dep in _list(package, "PlatformDependencies")}
    return (
        "Windows.Desktop" in platforms
        and _text(package, "PackageFormat").upper() == "MSIXVC"
        and "x64" in _list(package, "Architectures")
        and _family_name(package.get("MainPackageFamilyNameForDlc"))
        and _content_id(package.get("ContentId"))
    )


def _content_id(raw: Any) -> bool:
    try:
        return isinstance(raw, str) and bool(uuid.UUID(raw))
    except ValueError:
        return False


def _pc_package_size(product: dict[str, Any]) -> tuple[bool, int | None]:
    """``(has an installable PC package, its largest known size)``."""
    found, size = False, None
    for availability in _list(product, "DisplaySkuAvailabilities"):
        props = _sku_properties(availability)
        if props.get("IsTrial") is True or props.get("IsPreOrder") is True:
            continue
        for package in _list(props, "Packages"):
            if _pc_package(package):
                found = True
                known = _size(package)
                if known is not None and (size is None or known > size):
                    size = known
    return found, size


#: Members followed per pass. A pass lists a handful; a larger list is
#: not a DLC pass and is not followed.
MAX_BUNDLE_MEMBERS = 48


def bundle_members(product: Any, pid: str) -> list[str]:
    """The add-ons a ``Durable`` pass grants, or [] when ``product`` is not one.

    Only a ``Durable`` with no package of its own is a pass. Members are
    its SKUs' ``BundledSkus[].BigId`` values, without itself, sorted.
    """
    if not isinstance(product, dict) or product.get("ProductId") != pid:
        return []
    if _text(product, "ProductType") != "Durable" or _pc_package_size(product)[0]:
        return []
    members: set[str] = set()
    for availability in _list(product, "DisplaySkuAvailabilities"):
        props = _sku_properties(availability)
        if _list(props, "Packages"):
            return []
        members |= _bundled_ids(props)
    members.discard(pid)
    return sorted(members) if len(members) <= MAX_BUNDLE_MEMBERS else []


def dlc_of(product: Any, pid: str) -> DlcInfo | None:
    """The installable DLC ``product`` (id ``pid``) is, or None."""
    if not isinstance(product, dict) or product.get("ProductId") != pid:
        return None
    if _text(product, "ProductType") != "Durable":
        return None
    parent = addon_parent(product)
    if parent is None:
        return None
    found, size = _pc_package_size(product)
    if not found:
        return None
    localized = (_list(product, "LocalizedProperties") or [{}])[0]
    return DlcInfo(product_id=pid, parent=parent,
                   title=_text(localized, "ProductTitle") or pid, size_bytes=size)
