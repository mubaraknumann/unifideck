"""Which other stores already hold a given Steam game.

The Steam Store ownership ribbon asks one question per store-page visit:
"the user is looking at Steam AppID N; on which non-Steam stores do they
already have it?" This module answers it by joining the live unified library
against the ``steam_real_appid`` mapping cache.

**Why on demand, not an index.** The mapping cache is written at three
different moments, and only one of them emits anything: the metadata phase
runs *after* ``sync_complete``, the metadata backfill announces itself, and
``compatibility/library.py::_persist_steam_real_appid`` writes silently. An
index invalidated by events would miss the last one forever. One pass over
~1,300 games is a few thousand dict lookups, which is cheap at human
navigation speed and always as fresh as the cache.

**Why the loop runs over the library, not the cache.** ``steam_real_appid``
has no TTL and is never pruned, so it still maps shortcuts for games that
left the library. Starting from ``get_all_games()`` means a stale mapping can
never match.

**Purchase indexes.** A store whose library mixes subscription titles with
purchases (Microsoft: xCloud lists Game Pass and owned games together) can
pass an authenticated :class:`PurchaseIndex`. Its library rows then count
as owned only when the index lists their product, and owned products that
are not in the library at all (an Xbox purchase that cannot stream) match by
the index's own Steam AppID. A product with a library row is represented by
that row only, so one product never answers twice. Without an index the
store behaves as before: every row is "playable", never "owned".

**Streaming is two facts.** An xCloud row streams either because the title
is in the Game Pass catalog or because the user owns it, and each row says
which (:data:`GAME_PASS_KEY`). An owned title outside Game Pass streams as
the user's own (``OwnedCopy.streams``). A Game Pass title is a Game Pass
copy, owned or not. A row whose reason is unknown is a neutral cloud copy.

**Verified access.** A row only says what the user could do at the last
sync, and a failed sync keeps the old rows. A caller can pass
:class:`RowAccess` per row of a store (``access``), checked against the
account's entitlements and subscription *now*. For such a store a row claims
only what its access says: owned on PC, installable on PC through Game Pass
(its own "installable" copy), or streamable. A row with no access entry, or
with none of the three, claims nothing.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from unifideck.core.game_identity import SteamApp, build_identity
from unifideck.core.steam_appid_map import read_positive_steam_appid
from unifideck.core.store_capabilities import SUBSCRIPTION_LIBRARY_STORES
from unifideck.core.types import GameTag
from unifideck.core.types.domain import Game

#: Rows that do not mean "you have the game". DLC is the dangerous one: the
#: mapping is a fuzzy title search, so a DLC titled like its base game can
#: resolve to the base game's AppID and claim ownership of it.
NOT_OWNERSHIP_TAGS = frozenset({GameTag.DLC.value, GameTag.DEMO.value, GameTag.BETA.value})

#: ``Game.metadata["ownership"]`` for a row the store granted on a
#: presumption rather than on evidence (Battle.net presumes a game account
#: for every free-to-play catalog program, ``stores/battlenet/library``).
#: It earns a library tile, never an "owned" claim on a purchase page.
PRESUMED_OWNERSHIP = "presumed"

#: Distinct owned titles kept per store. Two is enough to show an edition
#: mismatch; more would only crowd a chip.
_MAX_TITLES = 2

#: ``Game.metadata`` key on an xCloud row: True when the title is in the Game
#: Pass catalog, False when it is not (it streams because the user owns it).
#: Absent means unknown: the catalog lists could not be read, or the row was
#: synced before they were. See ``stores/microsoft/game_pass.py``.
GAME_PASS_KEY = "game_pass"  # noqa: S105 — metadata key (Xbox Game Pass), not a credential


@dataclass(frozen=True)
class OwnedCopy:
    """One store's holding of the game.

    Attributes:
        store: store id (``"gog"``, ``"epic"``, …).
        titles: the distinct titles this store lists the game under, in
            library order, at most :data:`_MAX_TITLES`.
        installed: True if any copy from this store is installed.
        subscription: True for stores in
            :data:`~unifideck.core.store_capabilities.SUBSCRIPTION_LIBRARY_STORES`,
            whose rows are "playable", not "owned".
    """

    store: str
    titles: tuple[str, ...]
    installed: bool
    subscription: bool
    streams: bool = False
    """True if a library row of this store streams it (xCloud). On an owned
    copy, only when it streams as the user's own game, not via Game Pass."""
    game_pass: bool = False
    """On a subscription copy: True when the title is in the Game Pass
    catalog. False means it streams for a reason we cannot name."""
    platform: str = ""
    """For an indexed purchase: ``pc``, ``console``, ``pc_console`` or
    ``play_anywhere``; ``""`` when unknown or not indexed."""
    gold: bool = False
    """Every owned product is a Games with Gold grant (needs a subscription)."""
    installable: bool = False
    """On a subscription copy: installable on PC through Game Pass, not
    streamed. Its own ribbon line."""


@dataclass(frozen=True)
class RowAccess:
    """What one library row lets the user do now (see "Verified access").

    Attributes:
        owned_pc: a current PC licence the user bought or claimed.
        pass_pc: installable on PC through a Game Pass plan that has not ended.
        streams: streamable now (a recent entitlement, a usable plan).
        product_ids: every UPPER-case store product id the row stands for,
            so a purchase index finds the row under any of them.
    """

    owned_pc: bool = False
    pass_pc: bool = False
    streams: bool = False
    product_ids: frozenset[str] = frozenset()


_NO_ACCESS = RowAccess()


@dataclass(frozen=True)
class PurchasedProduct:
    """One product in a store's purchase index."""

    title: str
    pc: bool = False
    console: bool = False
    play_anywhere: bool = False
    gold: bool = False


@dataclass(frozen=True)
class PurchaseIndex:
    """A store's authenticated purchase list, shaped for the join.

    Attributes:
        products: by UPPER-case store product id (``Game.store_game_id``).
        by_steam_appid: real Steam AppID → the product ids mapped to it.
    """

    products: Mapping[str, PurchasedProduct] = field(default_factory=dict)
    by_steam_appid: Mapping[int, tuple[str, ...]] = field(default_factory=dict)


def platform_label(products: Sequence[PurchasedProduct]) -> str:
    """Where the user can play the owned copies: one ``OwnedCopy.platform`` value.

    One product that runs on both (Xbox Play Anywhere, or a single product
    shipping both packages) is ``play_anywhere``; separate PC and console
    purchases are ``pc_console``.
    """
    if any(p.play_anywhere or (p.pc and p.console) for p in products):
        return "play_anywhere"
    pc = any(p.pc for p in products)
    console = any(p.console for p in products)
    if pc and console:
        return "pc_console"
    return "pc" if pc else "console" if console else ""


def find_owned_copies(
    games: Iterable[Game], cache: Any, steam_app_id: int,
    purchases: Mapping[str, PurchaseIndex] | None = None,
    *,
    owned_steam: Sequence[SteamApp] = (),
    steam_name: str = "",
    access: Mapping[str, Mapping[str, RowAccess]] | None = None,
) -> list[OwnedCopy]:
    """Every non-Steam store holding *steam_app_id*, purchases first.

    A library row holds the game when it is any version of it, by the same
    rule the library's duplicate grouping uses
    (:mod:`unifideck.core.game_identity`): the Mass Effect (2007) page
    lists an owned "Mass Effect Legendary Edition". The owned title is
    shown when it differs from the page's, so the user sees which version.

    Args:
        games: the unified library (``sync_service.get_all_games()``).
        cache: the ``CacheManager``; a cold or raising cache yields no
            mappings, so only title matches remain.
        steam_app_id: the real Steam AppID the store page shows.
        purchases: authenticated purchase indexes by store id (see the
            module docstring); ``None`` or a missing store means none.
        owned_steam: the user's owned Steam games, which anchor versions
            whose rows map to a different Steam app.
        steam_name: the page's Steam name, so an unowned page still finds
            other versions by title.
        access: verified :class:`RowAccess` by store id, then by UPPER-case
            ``store_game_id``. A store listed here claims only what its rows'
            access says; other stores behave as without it.

    Returns:
        One :class:`OwnedCopy` per store, sorted purchased before
        subscription, then installed first, then by store id.
    """
    if steam_app_id <= 0:
        return []
    purchases = purchases or {}
    access = access or {}
    games = list(games)
    same_game = _same_game_rows(games, cache, steam_app_id, owned_steam, steam_name)
    matched, library_ids = _library_matches(games, same_game, purchases, access)
    copies: list[OwnedCopy] = []
    for store in sorted(set(matched) | set(purchases)):
        rows, index, ids = matched.get(store, []), purchases.get(store), library_ids.get(store, set())
        if store in access:
            copies += _verified_copies(store, rows, index, ids, steam_app_id, access[store])
        else:
            copies += _store_copies(store, rows, index, ids, steam_app_id)
    copies.sort(key=lambda c: (c.subscription, not c.installed, c.store))
    return copies


def _same_game_rows(
    games: list[Game], cache: Any, steam_app_id: int,
    owned_steam: Sequence[SteamApp], steam_name: str,
) -> set[int]:
    """``id()`` of every row that is a version of Steam app *steam_app_id*."""
    owned = list(owned_steam)
    if steam_name and all(app.appid != steam_app_id for app in owned):
        owned.append(SteamApp(steam_app_id, steam_name))
    identity = build_identity(
        [game.title or "" for game in games],
        [read_positive_steam_appid(cache, game.app_id) or None for game in games],
        owned,
    )
    return {id(games[i]) for i in identity.rows_for_steam_app(steam_app_id)}


def _library_matches(
    games: Iterable[Game], same_game: set[int],
    purchases: Mapping[str, PurchaseIndex],
    access: Mapping[str, Mapping[str, RowAccess]],
) -> tuple[dict[str, list[Game]], dict[str, set[str]]]:
    """Rows of the game per store, and every product id of the indexed
    stores' libraries (an indexed product with a row is that row's)."""
    matched: dict[str, list[Game]] = {}
    library_ids: dict[str, set[str]] = {store: set() for store in purchases}
    for game in games:
        if not _counts_as_ownership(game):
            continue
        if game.store in library_ids:
            library_ids[game.store].add(_product_id(game))
            if game.store in access:
                library_ids[game.store] |= _access_of(game, access[game.store]).product_ids
        if id(game) in same_game:
            matched.setdefault(game.store, []).append(game)
    return matched, library_ids


def _store_copies(
    store: str, rows: list[Game], index: PurchaseIndex | None,
    library_ids: set[str], steam_app_id: int,
) -> list[OwnedCopy]:
    """One store's answer: what the user owns there, then how it streams.

    Without an index, or when the index lists none of it, every row is
    "playable" (one subscription copy). With owned products there is an
    owned copy, plus a subscription copy when streaming is not explained by
    the purchase: see :func:`_stream_copy`.
    """
    owned_rows = [r for r in rows if index and _product_id(r) in index.products]
    owned = _owned_products(owned_rows, index, library_ids, steam_app_id)
    if not owned:
        if not rows:
            return []
        return [_merge(store, rows, streams=any(map(_streams, rows)),
                       game_pass=any(_game_pass(r) is True for r in rows))]
    copy = OwnedCopy(
        store=store,
        titles=_titles([r.title or "" for r in owned_rows] + [p.title for p in owned]),
        installed=any(r.installed for r in owned_rows),
        subscription=False,
        streams=any(_streams(r) and _game_pass(r) is False for r in owned_rows),
        platform=platform_label(owned),
        gold=all(p.gold for p in owned),
    )
    stream = _stream_copy(store, rows, {id(r) for r in owned_rows})
    return [copy, stream] if stream else [copy]


def _owned_products(
    owned_rows: list[Game], index: PurchaseIndex | None,
    library_ids: set[str], steam_app_id: int,
) -> list[PurchasedProduct]:
    """The index's products for the owned rows, plus owned products with no
    library row that the index itself maps to *steam_app_id*."""
    if index is None:
        return []
    extra_ids = [
        pid for pid in index.by_steam_appid.get(steam_app_id, ())
        if pid not in library_ids and pid in index.products
    ]
    return (
        [index.products[_product_id(r)] for r in owned_rows]
        + [index.products[pid] for pid in extra_ids]
    )


def _verified_copies(
    store: str, rows: list[Game], index: PurchaseIndex | None,
    library_ids: set[str], steam_app_id: int, access: Mapping[str, RowAccess],
) -> list[OwnedCopy]:
    """One store's answer from verified access: owned, then installable
    through Game Pass, then streamable. A row that is none of these (a
    lapsed plan, a stale row, an unsupported subscription) claims nothing."""
    owned_rows: list[Game] = []
    owned: list[PurchasedProduct] = []
    for row in rows:
        product = _verified_product(row, _access_of(row, access), index)
        if product is not None:
            owned_rows.append(row)
            owned.append(product)
    owned += _owned_products([], index, library_ids, steam_app_id)
    owned_ids = {id(r) for r in owned_rows}
    copies: list[OwnedCopy] = []
    if owned:
        copies.append(OwnedCopy(
            store=store,
            titles=_titles([r.title or "" for r in owned_rows] + [p.title for p in owned]),
            installed=any(r.installed for r in owned_rows),
            subscription=False,
            streams=any(
                _access_of(r, access).streams and _game_pass(r) is False for r in owned_rows
            ),
            platform=platform_label(owned),
            gold=all(p.gold for p in owned),
        ))
    installable = [
        r for r in rows if _access_of(r, access).pass_pc and id(r) not in owned_ids
    ]
    if installable:
        copies.append(OwnedCopy(
            store=store, titles=_titles(r.title or "" for r in installable),
            installed=any(r.installed for r in installable),
            subscription=True, game_pass=True, installable=True,
        ))
    streaming = [r for r in rows if _access_of(r, access).streams]
    stream = _stream_copy(store, streaming, owned_ids)
    return [*copies, stream] if stream else copies


def _verified_product(
    row: Game, access: RowAccess, index: PurchaseIndex | None,
) -> PurchasedProduct | None:
    """The purchase behind *row*: its index product (on PC too when the PC
    licence is verified), a PC product for a verified licence the index
    lacks, or None when the user does not own it."""
    indexed = next(
        (index.products[pid] for pid in sorted({_product_id(row)} | access.product_ids)
         if index is not None and pid in index.products),
        None,
    )
    if indexed is not None:
        return dataclasses.replace(indexed, pc=True) if access.owned_pc else indexed
    if access.owned_pc:
        return PurchasedProduct(title=(row.title or "").strip(), pc=True)
    return None


def _access_of(game: Game, access: Mapping[str, RowAccess]) -> RowAccess:
    return access.get(_product_id(game), _NO_ACCESS)


def _stream_copy(store: str, rows: list[Game], owned_ids: set[int]) -> OwnedCopy | None:
    """The subscription copy next to an owned one, or None.

    A Game Pass title gets a Game Pass copy, even when owned: owning it does
    not say it would stream without the subscription. Otherwise a streaming
    row the purchase does not explain (catalog flag unknown, or neither owned
    nor in Game Pass) gets a neutral copy. An owned row outside Game Pass
    needs neither: the owned copy's ``streams`` says it.
    """
    streaming = [r for r in rows if _streams(r)]
    game_pass = [r for r in streaming if _game_pass(r) is True]
    if game_pass:
        return _merge(store, game_pass, streams=True, game_pass=True)
    unexplained = [
        r for r in streaming
        if _game_pass(r) is None or id(r) not in owned_ids
    ]
    return _merge(store, unexplained, streams=True) if unexplained else None


def _streams(game: Game) -> bool:
    return GameTag.XCLOUD.value in map(str, game.tags or ())


def _game_pass(game: Game) -> bool | None:
    """The row's catalog flag (:data:`GAME_PASS_KEY`); None when unknown."""
    value = (game.metadata or {}).get(GAME_PASS_KEY)
    return value if isinstance(value, bool) else None


def _product_id(game: Game) -> str:
    return (game.store_game_id or "").upper()


def _titles(candidates: Iterable[str]) -> tuple[str, ...]:
    titles: list[str] = []
    for raw in candidates:
        title = raw.strip()
        if title and title not in titles and len(titles) < _MAX_TITLES:
            titles.append(title)
    return tuple(titles)


def _counts_as_ownership(game: Game) -> bool:
    """Whether *game* is a non-Steam row that means "you have it"."""
    if not game.store or game.store == "steam":
        return False
    if (game.metadata or {}).get("ownership") == PRESUMED_OWNERSHIP:
        return False
    return not any(str(tag) in NOT_OWNERSHIP_TAGS for tag in game.tags or ())


def _merge(
    store: str, rows: list[Game], *, streams: bool = False, game_pass: bool = False,
) -> OwnedCopy:
    """Collapse one store's rows (editions, duplicates) into one copy."""
    return OwnedCopy(
        store=store,
        titles=_titles(row.title or "" for row in rows),
        installed=any(row.installed for row in rows),
        subscription=store in SUBSCRIPTION_LIBRARY_STORES,
        streams=streams,
        game_pass=game_pass,
    )
