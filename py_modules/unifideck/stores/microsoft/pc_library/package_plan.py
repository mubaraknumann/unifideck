"""Which package of a Store product Xodus would install: the package plan.

A port of ``crates/xodus-cli/src/package_plan.rs`` from our build's
``xodus-package-selection`` patch, so the library can tell ``ready``,
``choice`` and ``unavailable`` titles apart for hundreds of products with
batched displaycatalog reads instead of one ``resolve-package`` per title.

The rules are the patch's:

- A package is supported only when it is ``Windows.Desktop``, ``MSIXVC``,
  the product is a ``Game`` or ``GameAddOn``, and its SKU is neither a
  trial nor a pre-order. The same content id can sit in several SKUs; a
  supported copy always wins.
- Children: a product with its own desktop package is defined by it.
  Otherwise its ``BundledSkus`` are followed, and only without those its
  ``big:`` licensing keys (never itself, never a ``CFQ...`` plan).
- Limits: depth 6, 48 products, 128 choices. A cycle, a missing child or an
  id mismatch fails without guessing.
- ``ready`` is exactly one supported choice that is a ``Game``. ``choice``
  is any other set with a supported choice. ``unavailable`` has none.

Install still runs ``resolve-package`` (stage C): this plan decides what is
listed, the CLI decides what is downloaded.
"""
from __future__ import annotations

import asyncio
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .entitlements import PLAN_PREFIX, is_product_id

MAX_PRODUCTS = 48
MAX_DEPTH = 6
MAX_CHOICES = 128
_TITLE_CHARS = 240
#: Catalog languages, as the patch asks for them.
CATALOG_LANGUAGES = "en-US,neutral"

FetchDetailed = Callable[..., Awaitable[tuple[dict[str, dict[str, Any]], list[str]]]]


class PlanError(ValueError):
    """The product's metadata does not give a safe plan."""


class MissingProductError(PlanError):
    """A product the plan needs was not fetched. ``product_id`` names it."""

    def __init__(self, product_id: str) -> None:
        super().__init__(f"incomplete Store bundle metadata: {product_id}")
        self.product_id = product_id


#: Short name used in the docs and the design.
MissingProduct = MissingProductError


@dataclass(frozen=True)
class Choice:
    """One package of a plan. ``size_bytes`` is None when the catalog has none."""

    product_id: str
    content_id: str
    title: str
    platform: str
    format: str
    kind: str
    supported: bool
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape kept in ``metadata["xbox_pc"]["choices"]``."""
        return {
            "content_id": self.content_id, "product_id": self.product_id,
            "title": self.title, "kind": self.kind, "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class Plan:
    """Every package the product resolves to, sorted by content id."""

    choices: tuple[Choice, ...]

    def supported(self) -> list[Choice]:
        """The choices Xodus can install."""
        return [c for c in self.choices if c.supported]

    def automatic(self) -> Choice | None:
        """The one choice to install without asking, or None."""
        eligible = self.supported()
        if len(eligible) == 1 and eligible[0].kind == "Game":
            return eligible[0]
        return None

    @property
    def status(self) -> str:
        """``ready``, ``choice`` or ``unavailable``."""
        if self.automatic() is not None:
            return "ready"
        return "choice" if self.supported() else "unavailable"


@dataclass(frozen=True)
class PlanProducts:
    """What :func:`fetch_plan_products` found, by upper-case product id.

    ``unknown`` ids were not answered (a failed batch or the deadline).
    ``absent`` ids were answered and the catalog has no such product.
    """

    products: dict[str, dict[str, Any]]
    unknown: set[str] = field(default_factory=set)
    absent: set[str] = field(default_factory=set)


def _text(value: Any, key: str) -> str:
    """A string field without control characters, at most 240 characters."""
    raw = value.get(key) if isinstance(value, dict) else None
    if not isinstance(raw, str):
        return ""
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Cc")[:_TITLE_CHARS]


def _list(value: Any, key: str) -> list[Any]:
    """A list field, or an empty list."""
    raw = value.get(key) if isinstance(value, dict) else None
    return raw if isinstance(raw, list) else []


def _size(package: dict[str, Any]) -> int | None:
    """``MaxDownloadSizeInBytes`` when it is a positive integer."""
    size = package.get("MaxDownloadSizeInBytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return size


def _package_choice(
    package: dict[str, Any], properties: dict[str, Any], pid: str, title: str, kind: str,
) -> Choice | None:
    """The choice for one package entry, or None without a valid content id."""
    raw = package.get("ContentId")
    try:
        content_id = str(uuid.UUID(raw)) if isinstance(raw, str) else ""
    except ValueError:
        content_id = ""
    if not content_id:
        return None
    platforms = [p for p in (_text(v, "PlatformName") for v in _list(package, "PlatformDependencies")) if p]
    desktop = "Windows.Desktop" in platforms
    fmt = _text(package, "PackageFormat").upper()
    supported = (
        desktop and fmt == "MSIXVC" and kind in ("Game", "GameAddOn")
        and properties.get("IsTrial") is not True
        and properties.get("IsPreOrder") is not True
    )
    return Choice(
        product_id=pid, content_id=content_id, title=title,
        platform="Windows.Desktop" if desktop else ", ".join(platforms),
        format=fmt, kind=kind, supported=supported, size_bytes=_size(package),
    )


def _licensed_id(key: Any, pid: str) -> str | None:
    """The product id in one ``big:<id>:...`` key, or None when not followed."""
    parts = key.split(":") if isinstance(key, str) else []
    if len(parts) != 3 or parts[0] != "big" or not is_product_id(parts[1]):
        return None
    if parts[1] == pid or parts[1].startswith(PLAN_PREFIX):
        return None
    return parts[1]


def _licensing_ids(availability: dict[str, Any], pid: str) -> set[str]:
    """``big:<id>:...`` keys of an offer, without ``pid`` itself or plans."""
    found: set[str] = set()
    for offer in _list(availability, "Availabilities"):
        licensing = offer.get("LicensingData") if isinstance(offer, dict) else None
        for satisfying in _list(licensing, "SatisfyingEntitlementKeys"):
            ids = (_licensed_id(key, pid) for key in _list(satisfying, "EntitlementKeys"))
            found.update(i for i in ids if i)
    return found


def _bundled_ids(properties: dict[str, Any]) -> set[str]:
    """``BundledSkus[].BigId`` values that are product ids."""
    return {
        ref["BigId"] for ref in _list(properties, "BundledSkus")
        if isinstance(ref, dict) and is_product_id(ref.get("BigId"))
    }


def _sku_properties(availability: Any) -> dict[str, Any]:
    """``Sku.Properties`` of one availability, or an empty dict."""
    sku = availability.get("Sku") if isinstance(availability, dict) else None
    properties = sku.get("Properties") if isinstance(sku, dict) else None
    return properties if isinstance(properties, dict) else {}


def _merge_packages(
    choices: dict[tuple[str, str, str], Choice],
    properties: dict[str, Any], pid: str, title: str, kind: str,
) -> None:
    """Add a SKU's package choices, keeping a supported copy already seen."""
    for package in _list(properties, "Packages"):
        choice = _package_choice(package, properties, pid, title, kind) if isinstance(package, dict) else None
        if choice is None:
            continue
        key = (choice.content_id, choice.platform, choice.format)
        if not (key in choices and choices[key].supported):
            choices[key] = choice


def _scan_skus(
    product: dict[str, Any], pid: str, title: str, kind: str,
) -> tuple[dict[tuple[str, str, str], Choice], set[str], set[str]]:
    """``(choices by (content id, platform, format), bundled ids, licensed ids)``.

    The same package can sit in pre-order, retail and trial SKUs; a
    supported copy is kept whatever the catalog order.
    """
    choices: dict[tuple[str, str, str], Choice] = {}
    bundled: set[str] = set()
    licensed: set[str] = set()
    for availability in _list(product, "DisplaySkuAvailabilities"):
        properties = _sku_properties(availability)
        _merge_packages(choices, properties, pid, title, kind)
        bundled |= _bundled_ids(properties)
        licensed |= _licensing_ids(availability, pid)
    return choices, bundled, licensed


def inspect(product: dict[str, Any], pid: str) -> tuple[list[Choice], list[str]]:
    """``(the product's own choices, the children to follow)``.

    Raises ``PlanError`` on an id mismatch, more than 128 choices or more
    than 48 children.
    """
    if not isinstance(product, dict) or product.get("ProductId") != pid:
        raise PlanError("Store product identity mismatch")
    localized = (_list(product, "LocalizedProperties") or [{}])[0]
    title = _text(localized, "ProductTitle") or pid
    choices, bundled, licensed = _scan_skus(product, pid, title, _text(product, "ProductType"))
    if len(choices) > MAX_CHOICES:
        raise PlanError("Too many Store packages")
    # Direct packages define this product: unrelated licensing keys are not followed.
    has_desktop = any(c.platform == "Windows.Desktop" for c in choices.values())
    children = [] if has_desktop else sorted(bundled or licensed)
    if len(children) > MAX_PRODUCTS:
        raise PlanError("Store bundle contains too many products")
    return [choices[k] for k in sorted(choices)], children


@dataclass
class _Walk:
    products: dict[str, dict[str, Any]]
    path: set[str] = field(default_factory=set)
    seen: set[str] = field(default_factory=set)
    choices: dict[str, Choice] = field(default_factory=dict)

    def visit(self, pid: str, depth: int) -> None:
        if depth > MAX_DEPTH or pid in self.path:
            raise PlanError("Circular or excessively deep Store bundle")
        if pid in self.seen:
            return
        if len(self.seen) >= MAX_PRODUCTS:
            raise PlanError("Store bundle contains too many products")
        self.path.add(pid)
        product = self.products.get(pid)
        if product is None:
            raise MissingProductError(pid)
        direct, children = inspect(product, pid)
        for choice in direct:
            old = self.choices.get(choice.content_id)
            if old is None or not old.supported:
                self.choices[choice.content_id] = choice
        if len(self.choices) > MAX_CHOICES:
            raise PlanError("Too many Store packages")
        for child in children:
            self.visit(child, depth + 1)
        self.path.discard(pid)
        self.seen.add(pid)


def build_plan(root: str, products: dict[str, dict[str, Any]]) -> Plan:
    """The plan of ``root`` from already fetched products (upper-case keys).

    Across products, choices are deduplicated on the content id alone.
    Raises ``MissingProduct`` when a needed product is not in ``products``,
    and ``PlanError`` for every other unsafe shape.
    """
    walk = _Walk(products)
    walk.visit(root, 0)
    return Plan(choices=tuple(walk.choices[k] for k in sorted(walk.choices)))


def _children_of(products: dict[str, dict[str, Any]], ids: list[str]) -> set[str]:
    """Children of the fetched ``ids``. A product that fails ``inspect`` has none."""
    found: set[str] = set()
    for pid in ids:
        try:
            found.update(inspect(products[pid], pid)[1])
        except PlanError:
            continue
    return found


async def _fetch_round(
    fetch: FetchDetailed, ids: list[str], market: str, wait_s: float,
) -> tuple[dict[str, dict[str, Any]], list[str]] | None:
    """One batched read, or None when it does not finish in ``wait_s``."""
    if wait_s <= 0:
        return None
    task = asyncio.ensure_future(fetch(ids, market, languages=CATALOG_LANGUAGES))
    done, _pending = await asyncio.wait({task}, timeout=wait_s)
    if task not in done:
        task.cancel()
        return None
    return task.result()


async def fetch_plan_products(
    roots: list[str], market: str, *, fetch: FetchDetailed, deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> PlanProducts:
    """Fetch ``roots`` and their bundle children, level by level.

    At most ``MAX_DEPTH + 1`` rounds. An id whose batch failed, or that was
    not fetched before ``deadline`` (a ``clock`` time), is unknown, never
    absent, so its last good plan can be reused.
    """
    found = PlanProducts(products={})
    pending = sorted(set(roots))
    for _round in range(MAX_DEPTH + 1):
        if not pending:
            break
        answer = await _fetch_round(fetch, pending, market, deadline - clock())
        if answer is None:
            found.unknown.update(pending)
            break
        products, failed = answer
        found.unknown.update(failed)
        got = [pid for pid in pending if pid in products]
        found.absent.update(p for p in pending if p not in products and p not in failed)
        found.products.update({pid: products[pid] for pid in got})
        pending = sorted(
            c for c in _children_of(found.products, got)
            if c not in found.products and c not in found.unknown and c not in found.absent
        )
    else:
        found.unknown.update(pending)
    return found
