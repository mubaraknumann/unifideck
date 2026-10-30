"""PC titles as library rows: classify, merge with xCloud, keep installed rows.

The order of a sync is :func:`classify` (entitlements and plans to titles),
then :func:`merge_with_xcloud`, then :func:`installed_floor`, then the shared
install overlay. Three rules hold throughout:

- **One row per Xbox game.** A PC title joins an xCloud row on an exact
  (upper-cased) product id: the install target first, then the entitlement.
  A title left over joins an xCloud row of the same game by name, under the
  narrow rule in :mod:`.title_pairs` (platform words ignored, editions
  kept, unique pairs only). The rest get their own row, and game grouping
  puts them side by side.
- **Never re-key a tile.** :class:`StickyIds` remembers which
  ``store_game_id`` each entitlement and target id was listed under, so a
  change of id case or of install target keeps the same shortcut.
- **Never drop an installed game.** Every install record keeps a row, even
  when the entitlement left the list or the folder is on an unmounted card.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from unifideck.core.cross_store_ownership import GAME_PASS_KEY
from unifideck.core.types import Game, GameTag

from .entitlements import (
    INSTALL_ACCESS,
    EntitlementSnapshot,
    access_of,
    is_product_id,
    plan_id_of,
)
from .package_plan import MissingProductError, Plan, PlanError, PlanProducts, build_plan
from .title_pairs import pair_by_title

META_KEY = "xbox_pc"
META_VERSION = 1
#: Dedupe priority of the access kinds (lower wins), then of the states.
_ACCESS_RANK = {"owned": 0, "gamepass": 1, "unknown_plan": 2, "ea_play": 3, "ubisoft_plus": 4}
_STATE_RANK = {"ready": 0, "choice": 1, "not_supported": 2}
_MAX_CHOICES = 128


@dataclass(frozen=True)
class PcTitle:
    """One PC title the account may list, keyed by its install target."""

    entitlement_id: str
    target_id: str
    title: str
    state: str
    reason: str | None
    access: str
    plan_id: str | None = None
    content_id: str | None = None
    choices: tuple[dict[str, Any], ...] = ()
    size_bytes: int | None = None
    end_date: str | None = None

    def to_meta(self, market: str) -> dict[str, Any]:
        """The ``metadata["xbox_pc"]`` dict of a row."""
        return {
            "v": META_VERSION, "state": self.state, "reason": self.reason,
            "access": self.access, "plan_id": self.plan_id,
            "product_id": self.entitlement_id, "target_id": self.target_id,
            "content_id": self.content_id, "choices": [dict(c) for c in self.choices],
            "size_bytes": self.size_bytes, "market": market,
            "end_date": self.end_date, "title": self.title,
        }

    @classmethod
    def from_meta(cls, meta: Any) -> PcTitle | None:
        """The title a stored meta dict describes, or None if it is invalid."""
        if not isinstance(meta, dict) or meta.get("v") != META_VERSION:
            return None
        ent, target = meta.get("product_id"), meta.get("target_id")
        if not is_product_id(ent) or not is_product_id(target):
            return None
        raw_choices = meta.get("choices")
        choices = raw_choices if isinstance(raw_choices, list) else []
        size = meta.get("size_bytes")
        return cls(
            entitlement_id=ent, target_id=target, title=str(meta.get("title") or target),
            state=str(meta.get("state") or "not_supported"), reason=meta.get("reason"),
            access=str(meta.get("access") or "none"), plan_id=meta.get("plan_id"),
            content_id=meta.get("content_id"),
            choices=tuple(c for c in choices if isinstance(c, dict)),
            size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
            end_date=meta.get("end_date"),
        )


# ── Plans ────────────────────────────────────────────────────────────────

def summarize(root: str, plan: Plan | None, product: Mapping[str, Any]) -> dict[str, Any]:
    """The cacheable summary of ``root``'s plan (``plan`` None = unavailable).

    The pick is the automatic choice, else the only supported ``Game``
    among add-ons. With no pick, the target is the root itself.
    """
    localized = product.get("LocalizedProperties")
    first = localized[0] if isinstance(localized, list) and localized else {}
    title = first.get("ProductTitle") if isinstance(first, dict) else None
    kind = product.get("ProductType")
    summary: dict[str, Any] = {
        "status": plan.status if plan else "unavailable",
        "kind": kind if isinstance(kind, str) else "",
        "title": title if isinstance(title, str) and title else root,
        "target_id": root, "content_id": None, "size_bytes": None, "choices": [],
    }
    if plan is None:
        return summary
    games = [c for c in plan.supported() if c.kind == "Game"]
    pick = plan.automatic() or (games[0] if len(games) == 1 else None)
    summary["choices"] = [c.to_dict() for c in plan.supported()][:_MAX_CHOICES]
    if pick is not None:
        summary.update(target_id=pick.product_id, content_id=pick.content_id,
                       size_bytes=pick.size_bytes)
    return summary


def summarize_roots(
    roots: Iterable[str], found: PlanProducts,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """``(summary per root, roots whose plan is unknown)``.

    A root the catalog does not have gets no summary and is not unknown.
    A plan that needs an unanswered product is unknown; one that needs an
    absent product, or is unsafe, is unavailable.
    """
    summaries: dict[str, dict[str, Any]] = {}
    unknown: set[str] = set()
    for root in roots:
        if root in found.unknown:
            unknown.add(root)
            continue
        product = found.products.get(root)
        if product is None:
            continue
        try:
            plan: Plan | None = build_plan(root, found.products)
        except MissingProductError as missing:
            if missing.product_id in found.unknown:
                unknown.add(root)
                continue
            plan = None
        except PlanError:
            plan = None
        summaries[root] = summarize(root, plan, product)
    return summaries, unknown


# ── Classify ─────────────────────────────────────────────────────────────

def _title_from(pid: str, access: str, summary: dict[str, Any], snap: EntitlementSnapshot) -> PcTitle:
    """The title of one entitlement with a listable plan."""
    content_id = summary.get("content_id")
    if access not in INSTALL_ACCESS:
        state, reason = "not_supported", access
    elif summary["status"] == "ready":
        state, reason = "ready", None
    else:
        state, reason = "choice", (None if content_id else "needs_choice")
    return PcTitle(
        entitlement_id=pid, target_id=str(summary.get("target_id") or pid),
        title=str(summary.get("title") or pid), state=state, reason=reason,
        access=access, plan_id=plan_id_of(pid, snap.entries), content_id=content_id,
        choices=tuple(summary.get("choices") or ()), size_bytes=summary.get("size_bytes"),
        end_date=snap.entries[pid].end_date_text,
    )


def _rank(title: PcTitle) -> tuple[int, int]:
    return _ACCESS_RANK.get(title.access, 9), _STATE_RANK.get(title.state, 9)


def dedupe_titles(titles: Iterable[PcTitle]) -> list[PcTitle]:
    """One title per install target: owned before Game Pass before the rest,
    then ready before choice. Sorted by target id."""
    best: dict[str, PcTitle] = {}
    for title in titles:
        old = best.get(title.target_id)
        if old is None or _rank(title) < _rank(old):
            best[title.target_id] = title
    return [best[k] for k in sorted(best)]


def classify(
    snap: EntitlementSnapshot, summaries: Mapping[str, dict[str, Any]],
    unknown_roots: set[str], now: float,
) -> tuple[list[PcTitle], set[str]]:
    """``(titles, entitlement ids whose plan is unknown)``.

    Entitlements without access, absent products, non-``Game`` products and
    ``unavailable`` plans produce no title. Access outside
    ``INSTALL_ACCESS`` gives ``not_supported`` with that access as reason.
    """
    titles: list[PcTitle] = []
    unknown: set[str] = set()
    for pid in sorted(snap.entries):
        access = access_of(pid, snap.entries, now)
        if access is None:
            continue
        if pid in unknown_roots:
            unknown.add(pid)
            continue
        summary = summaries.get(pid)
        if not summary or summary.get("kind") != "Game" or summary.get("status") == "unavailable":
            continue
        titles.append(_title_from(pid, access, summary, snap))
    return dedupe_titles(titles), unknown


# ── Rows ─────────────────────────────────────────────────────────────────

class StickyIds:
    """Which ``store_game_id`` each entitlement and target id was listed as.

    Keys are upper-case product ids. A record's ``storeGameId`` (the key the
    game was installed under) wins over the remembered map.
    """

    def __init__(self, ids: Mapping[str, str] | None = None) -> None:
        self._ids = {k.upper(): v for k, v in (ids or {}).items()
                     if isinstance(k, str) and isinstance(v, str) and v}
        #: Keys whose id comes from an install record, not just a past sync.
        self._pinned: set[str] = set()

    @classmethod
    def from_sources(cls, remembered: Mapping[str, str], records: Iterable[Mapping[str, Any]]) -> StickyIds:
        """The remembered map with every record's ``storeGameId`` on top."""
        sticky = cls(remembered)
        for record in records:
            sgid = record.get("storeGameId")
            key = str(record.get("productId") or "")
            if isinstance(sgid, str) and sgid and key:
                sticky.remember(key, sgid)
                sticky._pinned.add(key.upper())
        return sticky

    def pinned(self, *keys: str) -> str | None:
        """The id an install record fixed for the first such key, or None."""
        for key in keys:
            if key and key.upper() in self._pinned:
                return self._ids.get(key.upper())
        return None

    def lookup(self, *keys: str) -> str | None:
        """The id of the first key that has one."""
        for key in keys:
            sgid = self._ids.get(key.upper()) if key else None
            if sgid:
                return sgid
        return None

    def remember(self, key: str, sgid: str) -> None:
        if key:
            self._ids[key.upper()] = sgid

    def to_dict(self) -> dict[str, str]:
        return dict(self._ids)


def _game_pass(access: str) -> bool | None:
    """``GAME_PASS_KEY`` for an access kind: only owned and Game Pass are known."""
    return {"gamepass": True, "owned": False}.get(access)


def _merged(game: Game, title: PcTitle, sgid: str, market: str) -> Game:
    """An xCloud row that is also a PC title."""
    metadata = dict(game.metadata)
    metadata[META_KEY] = title.to_meta(market)
    if GAME_PASS_KEY not in metadata and _game_pass(title.access) is not None:
        metadata[GAME_PASS_KEY] = _game_pass(title.access)
    tags = list(game.tags) + ([] if GameTag.XBOX_PC in game.tags else [GameTag.XBOX_PC])
    return dataclasses.replace(game, store_game_id=sgid, tags=tags, metadata=metadata)


def _pc_only(title: PcTitle, sgid: str, market: str) -> Game:
    """A row for a PC title with no xCloud row."""
    metadata: dict[str, Any] = {META_KEY: title.to_meta(market)}
    if _game_pass(title.access) is not None:
        metadata[GAME_PASS_KEY] = _game_pass(title.access)
    return Game(
        app_id=0, store="microsoft", store_game_id=sgid, title=title.title,
        installed=False, tags=[GameTag.XBOX_PC], metadata=metadata,
    )


def _claim(xcloud: list[Game], titles: list[PcTitle]) -> tuple[dict[int, PcTitle], list[PcTitle]]:
    """Which xCloud row each title joins: target matches first, then
    entitlement matches. Returns ``(row index -> title, unmatched)``."""
    index = {g.store_game_id.upper(): i for i, g in enumerate(xcloud)}
    claims: dict[int, PcTitle] = {}
    rest: list[PcTitle] = []
    for title in titles:
        i = index.get(title.target_id)
        if i is not None and i not in claims:
            claims[i] = title
        else:
            rest.append(title)
    unmatched: list[PcTitle] = []
    for title in rest:
        i = index.get(title.entitlement_id)
        if i is not None and i not in claims:
            claims[i] = title
        else:
            unmatched.append(title)
    return claims, unmatched


def merge_with_xcloud(
    xcloud: list[Game], titles: list[PcTitle], sticky: StickyIds, market: str,
) -> list[Game]:
    """xCloud rows with PC titles merged in on exact ids, then PC-only rows.

    With no titles the xCloud list comes back unchanged.
    """
    claims, unmatched = _claim(xcloud, titles)
    pairs = pair_by_title(xcloud, set(range(len(xcloud))) - set(claims), unmatched)
    named = {row: unmatched[t] for row, t in pairs.items()}
    taken = set(pairs.values())
    unmatched = [t for i, t in enumerate(unmatched) if i not in taken]
    rows: list[Game] = []
    for i, game in enumerate(xcloud):
        title = claims.get(i)
        if title is not None:
            sgid = sticky.lookup(title.entitlement_id, title.target_id) or game.store_game_id
        elif i in named:
            # A name pair keeps the xCloud tile, unless the PC game is
            # installed: its record, prefix and games.map row use that id.
            title = named[i]
            sgid = sticky.pinned(title.entitlement_id, title.target_id) or game.store_game_id
        else:
            rows.append(game)
            continue
        rows.append(_merged(game, title, sgid, market))
    used = {g.store_game_id for g in rows}
    for title in unmatched:
        sgid = sticky.lookup(title.entitlement_id, title.target_id) or title.target_id
        if sgid in used:
            sgid = title.target_id
        if sgid in used:
            continue
        used.add(sgid)
        rows.append(_pc_only(title, sgid, market))
    return rows


def row_product_ids(game: Game) -> set[str]:
    """Every upper-case product id a row stands for."""
    ids = {game.store_game_id.upper()}
    meta = game.metadata.get(META_KEY)
    if isinstance(meta, dict):
        ids.update(str(meta.get(k) or "").upper() for k in ("product_id", "target_id"))
    ids.discard("")
    return ids


def _unconfirmed(pid: str, record: Mapping[str, Any], last: PcTitle | None, market: str) -> dict[str, Any]:
    """Meta for an installed game the current list does not confirm."""
    title = last.title if last else str(record.get("title") or pid)
    return PcTitle(
        entitlement_id=pid, target_id=pid, title=title, state="license_not_confirmed",
        reason="entitlement_missing", access="none",
        content_id=record.get("contentId") if isinstance(record.get("contentId"), str) else None,
    ).to_meta(str(record.get("market") or market))


def installed_floor(
    rows: list[Game], records: Mapping[str, Mapping[str, Any]], sticky: StickyIds,
    last_titles: Mapping[str, PcTitle], market: str,
) -> list[Game]:
    """``rows`` plus a row for every install record no PC row stands for.

    ``records`` is every record by product id, partial ones included, so a
    game on an unmounted card keeps its row (as not installed). A record
    that only matches an xCloud row upgrades that row instead of adding a
    second row with the same key.
    """
    out = list(rows)
    covered = {pid for g in out if GameTag.XBOX_PC in g.tags for pid in row_product_ids(g)}
    for pid in sorted(set(records) - covered):
        record = records[pid]
        meta = _unconfirmed(pid, record, last_titles.get(pid), market)
        sgid = sticky.lookup(pid) or pid
        hit = next((i for i, g in enumerate(out)
                    if g.store_game_id == sgid or g.store_game_id.upper() == pid), None)
        if hit is not None:
            game = out[hit]
            out[hit] = dataclasses.replace(
                game, tags=[*game.tags, GameTag.XBOX_PC],
                metadata={**game.metadata, META_KEY: meta},
            )
            continue
        out.append(Game(app_id=0, store="microsoft", store_game_id=sgid, title=meta["title"],
                        installed=False, tags=[GameTag.XBOX_PC], metadata={META_KEY: meta}))
    return out


def installed_overlay(
    rows: list[Game], verified: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """``{store_game_id: record}`` for the shared install overlay.

    Only PC rows take part, and only records that passed the disk check.
    """
    overlay: dict[str, Mapping[str, Any]] = {}
    for game in rows:
        if GameTag.XBOX_PC not in game.tags:
            continue
        for pid in sorted(row_product_ids(game)):
            if pid in verified:
                overlay[game.store_game_id] = verified[pid]
                break
    return overlay
