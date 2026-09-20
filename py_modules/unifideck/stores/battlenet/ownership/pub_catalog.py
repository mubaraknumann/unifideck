"""Load and merge Blizzard's cached PUB catalog.

py_modules/unifideck/stores/battlenet/ownership/pub_catalog.py

The client caches the PUB catalog as **plain JSON fragments** under
``AppData/Local/Battle.net/Cache/**``. Files are content-addressed (no
extension, hashed directory names), so they must be discovered by scanning.
A real prefix held **254 fragments**, 38 of which carried program rules.

Each fragment is a per-title record. The parts we need::

    {"fragment_id": "hearthstone",
     "program_configuration": {"WTCG": {"run_each_rule": [...]}},
     "products": [{"id": "WTCG", "base": {
        "program_id": "WTCG",              # the --exec launch code
        "title_id": 1465140039,            # joins to games-and-subs
        "name": "hearthstone#HS_NAME",     # a key into `strings`
        "default_product_type": "retail",
        "types": {"retail": {"uid": "hs_beta"}, "alpha": {"uid": "hs_alpha"}}}}],
     "strings": {"default": {"hearthstone#HS_NAME": "Hearthstone"}, ...}}

Three things here cost real debugging time and are worth stating plainly:

* **``types`` is the uid map; ``installs`` is only a fallback.**
  ``types["retail"]["uid"]`` gave ``hs_beta``, exactly the uid a real
  Hearthstone install used. ``installs`` lists every variant a title has
  (WoW has 45, including ``wow_ne_vendor11``), so it is consulted only when
  ``types`` carries no retail entry — which is real: no cached WoW fragment
  has one, and the fallback is what yields ``wow`` rather than
  ``wow_alpha``. Fragments are partial and repeat, so the maps are unioned
  across fragments rather than first-wins.
* **English is under locale ``default``**, not ``enUS`` — there is no
  ``enUS`` key at all.
* **A granted product id is not always a program id.** Rules can grant
  ``ARIS_Standard`` or ``WoWPTR``; those are variants of programs ``ARIS``
  and ``WoW``. Indexing by product id and resolving back to ``program_id``
  is what stops variants appearing as separate games.

Rule *evaluation* lives in ``rules.py``; this module only assembles inputs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

logger = logging.getLogger(__name__)

# Path of the client's HTTP cache relative to a prefix's drive_c.
CACHE_RELATIVE = "users/steamuser/AppData/Local/Battle.net/Cache"

# Cheap pre-filter: only catalog fragments carry this key.
_MARKER = b'"fragment_id"'

# A catalog fragment is small; anything larger is an asset.
MAX_FRAGMENT_BYTES = 4 * 1024 * 1024

# Blizzard's locale key for English. There is no 'enUS' in the catalog.
DEFAULT_LOCALE = "default"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One title, as the PUB catalog describes it."""

    product_id: str
    program_id: str
    name_key: str | None = None
    title_id: int | None = None
    default_product_type: str = "retail"
    # product type -> uid, e.g. {"retail": "hs_beta", "alpha": "hs_alpha"}
    type_uids: dict[str, str] = field(default_factory=dict)
    genre_key: str | None = None
    handheld_status: tuple[str, ...] = ()
    # Platforms the client will install this title on, e.g. ('win',). The
    # catalog pairs it with 'unsupported_platform_behavior': 'disabled_install',
    # so Blizzard itself gates installs on this field.
    supported_platforms: tuple[str, ...] = ()
    # Every uid the client can install for this title. Fallback only: WoW
    # lists 45 of these (including 'wow_ne_vendor11'), so `types` is
    # authoritative wherever it answers.
    install_uids: tuple[str, ...] = ()

    def runs_on_windows(self) -> bool:
        """True unless the catalog says this is a non-Windows title.

        Absent data answers True: a fragment that never carried platforms
        must not cost the user a game they own.
        """
        return not self.supported_platforms or "win" in self.supported_platforms

    def uid_for(self, product_type: str | None = None) -> str | None:
        """The uid to install/launch for a product type, defaulting to retail."""
        for candidate in (product_type, self.default_product_type, "retail"):
            if candidate and candidate in self.type_uids:
                return self.type_uids[candidate]
        return self._fallback_uid() or next(iter(self.type_uids.values()), None)

    def _fallback_uid(self) -> str | None:
        """Used when `types` has no retail entry — WoW is the real case.

        Prefer the uid that is exactly the lowercased program id ('wow'),
        which is the retail install; never guess by taking the first key,
        since those are ordered arbitrarily and start at 'wow_alpha'.
        """
        if not self.install_uids:
            return None
        target = self.program_id.lower()
        if target in self.install_uids:
            return target
        variants = set(self.type_uids.values())
        plain = [u for u in self.install_uids if u not in variants]
        return min(plain, key=len) if plain else None


@dataclass(slots=True)
class MergedCatalog:
    """Every catalog fragment, merged into one queryable view."""

    program_configurations: dict[str, object] = field(default_factory=dict)
    entries: dict[str, CatalogEntry] = field(default_factory=dict)
    strings: dict[str, dict[str, str]] = field(default_factory=dict)
    fragment_count: int = 0

    def entry_for(self, product_id: str) -> CatalogEntry | None:
        """Resolve a granted product id, falling back to its program id."""
        entry = self.entries.get(product_id)
        if entry is not None:
            return entry
        # 'WoWPTR' / 'ARIS_Standard' are variants; find their program.
        for candidate in self.entries.values():
            if candidate.program_id == product_id:
                return candidate
        return None

    def text(self, key: str | None, locale: str = DEFAULT_LOCALE) -> str | None:
        """Resolve a catalog string key, falling back to the default locale."""
        if not key:
            return None
        for loc in (locale, DEFAULT_LOCALE):
            found = self.strings.get(loc, {}).get(key)
            if found:
                return found
        return None

    def display_name(self, product_id: str, locale: str = DEFAULT_LOCALE) -> str | None:
        entry = self.entry_for(product_id)
        return self.text(entry.name_key, locale) if entry else None


def _iter_candidate_files(cache_dir: Path) -> Iterator[Path]:
    for path in cache_dir.rglob("*"):
        try:
            if path.is_file() and path.stat().st_size <= MAX_FRAGMENT_BYTES:
                yield path
        except OSError:
            continue


def _load_fragments(cache_dir: Path) -> Iterator[dict[str, object]]:
    for path in _iter_candidate_files(cache_dir):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if _MARKER not in raw:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("fragment_id"):
            yield payload


def _type_uids(base: dict[str, object]) -> dict[str, str]:
    types = base.get("types")
    if not isinstance(types, dict):
        return {}
    out: dict[str, str] = {}
    for product_type, cfg in types.items():
        if isinstance(product_type, str) and isinstance(cfg, dict):
            uid = cfg.get("uid")
            if isinstance(uid, str) and uid:
                out[product_type] = uid
    return out


def _install_uids(fragment: dict[str, object]) -> tuple[str, ...]:
    installs = fragment.get("installs")
    if not isinstance(installs, dict):
        return ()
    return tuple(u for u in installs if isinstance(u, str))


def _entry_from_product(product: object) -> CatalogEntry | None:
    if not isinstance(product, dict):
        return None
    base = product.get("base")
    if not isinstance(base, dict):
        return None
    product_id = product.get("id")
    program_id = base.get("program_id")
    if not isinstance(program_id, str) or not program_id:
        return None
    if not isinstance(product_id, str) or not product_id:
        product_id = program_id
    handheld = base.get("handheld_status")
    platforms = base.get("supported_platforms")
    title_id = base.get("title_id")
    return CatalogEntry(
        product_id=product_id,
        program_id=program_id,
        name_key=base.get("name") if isinstance(base.get("name"), str) else None,
        title_id=title_id if isinstance(title_id, int) else None,
        default_product_type=str(base.get("default_product_type") or "retail"),
        type_uids=_type_uids(base),
        genre_key=base.get("genre") if isinstance(base.get("genre"), str) else None,
        handheld_status=tuple(h for h in (handheld or []) if isinstance(h, str)),
        supported_platforms=tuple(p for p in (platforms or []) if isinstance(p, str)),
    )


def _absorb_entry(catalog: MergedCatalog, entry: CatalogEntry) -> None:
    """Fold one product record into the catalog, unioning what it knows.

    Fragments are partial and repeat across cache generations: the WoW
    record appears once carrying only alpha/beta/ptr uids and again with
    retail. Keeping whichever arrived first produced ``wow_alpha`` as the
    launch uid, so the maps are unioned rather than replaced, and scalar
    fields fill in only where currently unknown.
    """
    existing = catalog.entries.get(entry.product_id)
    if existing is None:
        catalog.entries[entry.product_id] = entry
        return
    merged_types = {**entry.type_uids, **existing.type_uids}
    catalog.entries[entry.product_id] = CatalogEntry(
        product_id=existing.product_id,
        program_id=existing.program_id or entry.program_id,
        name_key=existing.name_key or entry.name_key,
        title_id=existing.title_id if existing.title_id is not None else entry.title_id,
        default_product_type=existing.default_product_type or entry.default_product_type,
        type_uids=merged_types,
        genre_key=existing.genre_key or entry.genre_key,
        handheld_status=existing.handheld_status or entry.handheld_status,
        supported_platforms=existing.supported_platforms or entry.supported_platforms,
        install_uids=existing.install_uids
        + tuple(u for u in entry.install_uids if u not in existing.install_uids),
    )


def _merge_strings(catalog: MergedCatalog, fragment: dict[str, object]) -> None:
    strings = fragment.get("strings")
    if not isinstance(strings, dict):
        return
    for locale, table in strings.items():
        if isinstance(locale, str) and isinstance(table, dict):
            bucket = catalog.strings.setdefault(locale, {})
            for key, text in table.items():
                if isinstance(key, str) and isinstance(text, str):
                    bucket[key] = text


def _merge_programs(catalog: MergedCatalog, fragment: dict[str, object]) -> None:
    config = fragment.get("program_configuration")
    if not isinstance(config, dict):
        return
    for family, cfg in config.items():
        if isinstance(family, str):
            catalog.program_configurations.setdefault(family, cfg)


def _merge_products(catalog: MergedCatalog, fragment: dict[str, object]) -> None:
    uids = _install_uids(fragment)
    products = fragment.get("products")
    for product in products if isinstance(products, list) else []:
        entry = _entry_from_product(product)
        if entry is None:
            continue
        _absorb_entry(catalog, replace(entry, install_uids=uids) if uids else entry)


def merge_fragments(fragments: Iterator[dict[str, object]]) -> MergedCatalog:
    """Fold fragments into one catalog. Never raises."""
    catalog = MergedCatalog()
    for fragment in fragments:
        catalog.fragment_count += 1
        _merge_programs(catalog, fragment)
        _merge_products(catalog, fragment)
        _merge_strings(catalog, fragment)
    return catalog


def load_catalog(cache_dir: Path) -> MergedCatalog:
    """Scan the client cache and merge every catalog fragment found."""
    cache = Path(cache_dir)
    if not cache.is_dir():
        return MergedCatalog()
    catalog = merge_fragments(_load_fragments(cache))
    logger.info(
        "[Battlenet] PUB catalog: %d fragments, %d programs, %d titles, %d locales",
        catalog.fragment_count,
        len(catalog.program_configurations),
        len(catalog.entries),
        len(catalog.strings),
    )
    return catalog


def read_catalog(drive_c: Path) -> MergedCatalog:
    """Load the merged catalog from a prefix's ``drive_c``."""
    return load_catalog(Path(drive_c) / CACHE_RELATIVE)
