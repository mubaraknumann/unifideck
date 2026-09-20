"""Build the Battle.net library from client-local state.

py_modules/unifideck/stores/battlenet/library.py

Joins what the client leaves on disk::

    licences (CachedData.db)  ─┐
                               ├─> PUB catalog rules ─> playable programs
    presumed game accounts    ─┘
                                        │
    aggregate.json + product.db ────────┴─> installed overlay

Licences alone miss every free-to-play and subscription title, because those
match on ``game_account`` rather than ``license_id``. **Game accounts are not
knowable here**: ``CachedData.db`` holds licences and a battle tag and nothing
else, and the web endpoint that carries them needs an OAuth token we cannot
ship. Measured on one real account, that gap cost 7 of 24 titles — WoW,
Hearthstone, Overwatch, Heroes of the Storm, Diablo Immortal and two more.

So :func:`grant_ownership` presumes a game account for every catalog program:
the gated set is exactly the titles any Battle.net account can install and
play. Two guards keep the presumption from inventing dead tiles — a program
with no install uid is skipped, and so is one the catalog gives no Windows
build.

The library is keyed on the **uid**, not the family code. A uid is stable
(``fenris`` has never changed) while Blizzard renames families — Diablo IV
went ``D4`` -> ``Fen`` in 2026 — and the Steam app id is derived from
``store_game_id``, so a re-key would silently orphan the user's shortcut,
playtime, categories and artwork.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.core.types.domain import Game

from .ownership import (
    AccountFacts,
    InstalledGame,
    MergedCatalog,
    evaluate_catalog,
    read_catalog,
    read_installed,
    read_licences,
)
from .ownership.pub_catalog import CatalogEntry
from .product_db import read_product_db

logger = logging.getLogger(__name__)

STORE_NAME = "battlenet"


def _tags(entry: CatalogEntry | None, free_to_play: bool) -> list[str]:
    tags: list[str] = []
    if free_to_play:
        tags.append("free_to_play")
    for status in entry.handheld_status if entry else ():
        # 'handheld_optimized' / 'handheld_compatible' / 'handheld_unsupported'
        tags.append(status)
    return tags


def _game_from(
    program: str,
    entry: CatalogEntry | None,
    catalog: MergedCatalog,
    installed: InstalledGame | None,
    *,
    free_to_play: bool,
    launcher_path: str,
    uid: str | None = None,
    presumed: bool = False,
) -> Game | None:
    # An explicit uid wins: an installed game the catalog does not describe
    # still has one, and deriving it from a missing entry would drop the
    # game the fallback exists to preserve.
    uid = uid or (entry.uid_for() if entry else None)
    if not uid:
        # No uid means nothing to install or launch. Surfacing it would put
        # a dead tile in the user's library.
        logger.info("[Battlenet] skipping %s — catalog has no install uid", program)
        return None

    name = catalog.display_name(program) or (installed.name if installed else None) or program
    from unifideck.services.shortcut.games_map import generate_app_id

    return Game(
        app_id=generate_app_id(launcher_path, f"{STORE_NAME}:{uid}"),
        store=STORE_NAME,
        store_game_id=uid,
        title=name,
        installed=bool(installed and installed.is_ready),
        install_path=installed.host_install_path if installed else None,
        exe_path=installed.host_exe_path if installed else None,
        size_bytes=(installed.total_bytes or 0) if installed else 0,
        tags=_tags(entry, free_to_play),
        icon_url=installed.logo_art_url if installed else None,
        hero_url=installed.box_art_url if installed else None,
        metadata={
            "family": program,
            # Diagnostic only, deliberately not a tag: tags render as pills
            # in the UI, and "presumed" is our bookkeeping, not the user's.
            "ownership": "presumed" if presumed else "granted",
            "title_id": entry.title_id if entry else None,
            "version": installed.version if installed else None,
            "last_played_ms": installed.last_played_ms if installed else None,
        },
    )


def family_updates(games: list[Game]) -> dict[str, dict[str, Any]]:
    """``uid -> {"family": …}`` for every game whose family the catalog knew.

    The family code is the ``--exec`` argument the client needs and it lives
    only here, in the catalog join — the launcher runs out-of-process and
    cannot recompute it. Persisting it at sync is what makes a game
    launchable *before* it is installed, and is the only writer that sees
    every title rather than just the one being installed.
    """
    updates: dict[str, dict[str, Any]] = {}
    for game in games:
        family = game.metadata.get("family") if game.metadata else None
        if isinstance(family, str) and family and game.store_game_id:
            updates[game.store_game_id] = {"family": family}
    return updates


def record_families(id_map: Any, games: list[Game]) -> int:
    """Persist each title's ``--exec`` family code. Returns how many changed.

    Best-effort by contract: an unwritable id map must not fail a library
    read, because an empty library is a far worse outcome than a launch that
    later reports a missing family.
    """
    try:
        return int(id_map.merge_many(family_updates(games)))
    except Exception:
        logger.exception("[Battlenet] could not record family codes")
        return 0


def family_from_catalog(catalog: MergedCatalog, uid: str) -> str | None:
    """The program id (family) whose install uid is ``uid``, or None.

    The catalog maps family -> uid, so going the other way means scanning.
    Only used on the install path, where a title may not have been through a
    sync yet; :func:`record_families` covers the whole library at once.
    """
    wanted = normalize_uid(uid)
    for entry in catalog.entries.values():
        candidate = entry.uid_for()
        if candidate and normalize_uid(candidate) == wanted:
            return entry.program_id
    return None


def normalize_uid(uid: str) -> str:
    """The join key for a Battle.net uid, case-folded.

    Blizzard's own catalog is internally inconsistent about uid case. The PUB
    fragments spell Diablo's retail uid ``D1``, Warcraft I's ``W1`` and
    Warcraft II's ``W2``, while everything the *client* writes — ``product.db``,
    ``aggregate.json``, the Agent logs — is lowercase throughout. Joining the
    two case-sensitively reports exactly those titles as never installed: a
    real Diablo install finished on disk at 13:04, ``detect()`` never fired
    because ``product.db`` says ``d1`` and we asked for ``D1``, and five
    minutes later the watchdog failed it with "The install was never finished
    in Battle.net".

    **Only the join is normalized.** The uid we emit as ``store_game_id`` keeps
    its original case, because that string is what every released user's Steam
    shortcut is keyed on (see :mod:`unifideck.services.shortcut.games_map`) —
    re-keying it would strand their playtime, categories and artwork. The id
    map keeps its case for the same reason and needs no change: it is looked up
    with the same catalog uid it was written with, so it is already
    self-consistent, and the out-of-process launcher reads it the same way.
    """
    return uid.lower()


def install_row_for(
    state: dict[str, InstalledGame], uid: str,
) -> InstalledGame | None:
    """Look one uid up in an :func:`install_state_by_uid` mapping.

    Exists so the lookup side of the join can only be spelled once. Both
    callers — the library's ``install_row`` and the install watcher's ``row``
    — must normalize identically, and a second hand-written ``state.get(...)``
    is how that silently stops being true.
    """
    return state.get(normalize_uid(uid))


def _index_by_uid(installed: dict[str, InstalledGame]) -> dict[str, InstalledGame]:
    """Re-key install state on uid.

    ``aggregate.json`` and ``product.db`` are keyed on the product CODE
    (``hsb``) while the catalog addresses titles by uid (``hs_beta``). The
    uid is the only field common to both, so the join has to go through it —
    matching on code silently reports every installed game as not installed.

    Keys are normalized through :func:`normalize_uid`; look them up with
    :func:`install_row_for`, never with a bare ``.get``.
    """
    by_uid: dict[str, InstalledGame] = {}
    for game in installed.values():
        if game.uid:
            by_uid[normalize_uid(game.uid)] = game
    return by_uid


async def install_row(
    game_id: str, prefix: Path | None,
) -> InstalledGame | None:
    """This game's row in the client's install records, or ``None``.

    Keyed on the uid asked for. The earlier form returned the *first* game
    in the prefix, which is only ever right by accident — a prefix that
    picked up a second Blizzard title reported that one's path and size
    under this game's id.

    Moved off the store (2026-08-26) for its LOC cap; it reads the same
    client state as everything else here. The caller resolves the prefix,
    since only it holds the id map.
    """
    from . import paths

    if prefix is None:
        return None
    drive_c = paths.drive_c(prefix)
    if drive_c is None:
        return None
    state = await asyncio.to_thread(install_state_by_uid, drive_c, prefix)
    return install_row_for(state, game_id)


def install_state_by_uid(drive_c: Path, prefix: Path) -> dict[str, InstalledGame]:
    """Install state for one prefix, keyed the way the rest of the code asks.

    The install watcher needs to ask about *one* uid — "is the title the user
    pressed Install on ready yet" — and must not re-derive the code→uid join
    to do it. Getting that join wrong reports every installed game as not
    installed, which is the regression ``_index_by_uid`` exists to prevent.
    """
    return _index_by_uid(read_install_state(drive_c, prefix))


def _presumed_facts(catalog: MergedCatalog, facts: AccountFacts) -> AccountFacts:
    """``facts`` with a game account assumed for every program in the catalog."""
    return AccountFacts(
        licence_ids=facts.licence_ids,
        game_account_programs=frozenset(catalog.program_configurations),
        flags=facts.flags,
    )


def grant_ownership(
    catalog: MergedCatalog, facts: AccountFacts,
) -> tuple[dict[str, frozenset[Any]], frozenset[str]]:
    """Programs the rules grant, plus the subset granted presumptively.

    Free-to-play and subscription titles match on ``game_account`` rather
    than ``license_id``, and there is no way to learn which game accounts
    this user holds: the client's ``CachedData.db`` carries licences but no
    game accounts, and the web endpoint needs an OAuth token we cannot ship.
    On one real account that cost 7 of 24 titles — WoW, Hearthstone,
    Overwatch, Heroes of the Storm, Diablo Immortal and two more.

    So a game account is *presumed* for every catalog program: any
    Battle.net account can install and play the gated set. Only program
    **keys** absent from the base result are added, never merged into one
    already granted, which is what makes this structurally unable to shrink
    the library: a ``not{game_account: …}`` branch or a ``run_first_rule``
    alternative can never be re-litigated for a title the licences already
    won, and a presumed ``play_for_free`` tag can never land on a title the
    user actually bought.

    Returns ``(granted, presumed)``; ``presumed`` is a subset of the keys of
    ``granted``.
    """
    base = evaluate_catalog(catalog.program_configurations, facts)
    if facts.game_account_programs:
        # Real facts beat a presumption; a future producer needs no change here.
        return base, frozenset()
    probe = evaluate_catalog(catalog.program_configurations, _presumed_facts(catalog, facts))
    presumed = frozenset(program for program in probe if program not in base)
    granted = dict(base)
    granted.update({program: probe[program] for program in presumed})
    return granted, presumed


def _log_ownership_inputs(
    catalog: MergedCatalog,
    facts: AccountFacts,
    granted: dict[str, frozenset[Any]],
    presumed: frozenset[str],
) -> None:
    """Log every input the library size is a function of, once per sync.

    A user whose Battle.net library came back with one game (GitHub #447) had
    no way to say *which* of the three inputs was short, and neither did we:
    the catalog read, the account facts and the granted set were all silent.
    Each of these is a plain count, so this is cheap enough to run every sync
    and is the only thing that distinguishes an always-empty fact source
    (``flags`` has no producer) from a PUB cache the client had not finished
    writing when the first sync ran.

    A real prefix measures ~254 fragments, 38 of them carrying program rules;
    a first sync racing the client sees far fewer. Print both so the two are
    told apart from the log alone, without a second round trip to the reporter.
    """
    logger.info(
        "[Battlenet] ownership facts: licences=%d game_accounts=%d flags=%d",
        len(facts.licence_ids), len(facts.game_account_programs),
        len(facts.flags),
    )
    logger.info(
        "[Battlenet] PUB catalog: fragments=%d programs=%d titles=%d "
        "-> granted=%d",
        catalog.fragment_count, len(catalog.program_configurations),
        len(catalog.entries), len(granted),
    )
    if presumed:
        # Name them: a report of "a game I don't own appeared" is otherwise
        # a second round trip to find out which presumption produced it.
        logger.info(
            "[Battlenet] %d program(s) granted presumptively (game-account "
            "gated; any Battle.net account can play these): %s",
            len(presumed), ", ".join(sorted(presumed)),
        )


def build_library(
    catalog: MergedCatalog,
    facts: AccountFacts,
    installed: dict[str, InstalledGame],
    *,
    launcher_path: str,
) -> list[Game]:
    """Join ownership, catalog metadata and install state into Games."""
    granted, presumed = grant_ownership(catalog, facts)
    _log_ownership_inputs(catalog, facts, granted, presumed)
    by_uid = _index_by_uid(installed)
    games = _granted_games(granted, catalog, by_uid, launcher_path, presumed=presumed)
    seen = {normalize_uid(g.store_game_id) for g in games}
    games.extend(_orphan_installed(installed, catalog, seen, launcher_path))
    return games


def _granted_game(
    program: str,
    products: frozenset[Any],
    catalog: MergedCatalog,
    by_uid: dict[str, InstalledGame],
    launcher_path: str,
    *,
    presumed: bool,
) -> Game | None:
    entry = catalog.entry_for(program)
    if presumed and entry is not None and not entry.runs_on_windows():
        # A presumption must not invent a tile for a title the client will
        # not even install here (mobile- and macOS-only records exist).
        logger.info("[Battlenet] skipping %s — catalog lists no Windows build", program)
        return None
    uid = entry.uid_for() if entry else None
    return _game_from(
        program,
        entry,
        catalog,
        install_row_for(by_uid, uid) if uid else None,
        free_to_play=any(p.is_free_to_play for p in products),
        launcher_path=launcher_path,
        presumed=presumed,
    )


def _granted_games(
    granted: dict[str, frozenset[Any]],
    catalog: MergedCatalog,
    by_uid: dict[str, InstalledGame],
    launcher_path: str,
    *,
    presumed: frozenset[str] = frozenset(),
) -> list[Game]:
    """One Game per granted program, deduped by install uid.

    Owned programs are processed before presumed ones, and a uid already
    claimed is skipped: two program keys can resolve to the same entry (a
    variant resolves to its parent), and two Games sharing a ``store_game_id``
    derive the same Steam app id — one shortcut fighting itself.
    """
    games: list[Game] = []
    seen: set[str] = set()
    for program in sorted(granted, key=lambda p: (p in presumed, p)):
        game = _granted_game(
            program, granted[program], catalog, by_uid, launcher_path,
            presumed=program in presumed,
        )
        if game is None or normalize_uid(game.store_game_id) in seen:
            continue
        seen.add(normalize_uid(game.store_game_id))
        games.append(game)
    return games


def _orphan_installed(
    installed: dict[str, InstalledGame],
    catalog: MergedCatalog,
    seen_uids: set[str],
    launcher_path: str,
) -> list[Game]:
    """Installed titles the rules did not grant.

    They must not vanish: an ownership hiccup would otherwise take the
    user's installed game — and its Steam shortcut — with it.
    """
    games: list[Game] = []
    for code, state in installed.items():
        if not state.is_ready:
            continue
        entry = catalog.entry_for(code)
        uid = state.uid or (entry.uid_for() if entry else None) or code
        # Normalized both sides: the granted tile carries the catalog's ``D1``
        # while ``state.uid`` is the client's ``d1``, and a case-sensitive
        # miss here re-adds the same game as a second tile — with a family
        # taken from the product code, which launches nothing.
        if normalize_uid(uid) in seen_uids:
            continue
        logger.info(
            "[Battlenet] %s is installed but not granted by the rules — "
            "keeping it in the library", code,
        )
        game = _game_from(
            entry.program_id if entry else code,
            entry, catalog, state,
            free_to_play=False, launcher_path=launcher_path, uid=uid,
        )
        if game is not None:
            games.append(game)
    return games


def read_account_facts(drive_c: Path) -> AccountFacts:
    """Assemble the account facts the catalog rules are evaluated against.

    Licences are the only fact the client stores locally; game accounts are
    supplied by :func:`grant_ownership`'s presumption instead of read here.
    """
    licences = read_licences(drive_c)
    return AccountFacts(licence_ids=licences.licence_ids)


async def read_library(
    drive_c: Path,
    *,
    collect_installed: Callable[[], dict[str, Any]],
    launcher_path: str,
) -> list[Game] | None:
    """Read the whole library off client-local state, or ``None``.

    Split out of ``store.get_library`` (2026-08-26) for the store file's
    LOC cap; it belongs next to :func:`build_library` anyway, since every
    step is a read of the same client state.

    ``None`` means *we could not read*, which is a different claim from
    *you own nothing*: an empty list is authoritative downstream and lets
    the shortcut reconcile delete every Battle.net shortcut the user has
    (audit §3.5, finding B). Both unreadable cases return it — a missing
    prefix, and a catalog cache the client has not populated, without
    which every ownership rule has nothing to match against.

    Every read is filesystem/SQLite work, so each runs off the loop.
    """
    catalog = await asyncio.to_thread(read_catalog, drive_c)
    if not catalog.program_configurations:
        logger.warning(
            "[Battlenet] PUB catalog cache is empty — launch the client "
            "once so it populates; library reported unknown, not empty",
        )
        return None
    facts = await asyncio.to_thread(read_account_facts, drive_c)
    installed = await asyncio.to_thread(collect_installed)
    return build_library(
        catalog, facts, installed, launcher_path=launcher_path,
    )


def read_install_state(drive_c: Path, prefix: Path) -> dict[str, InstalledGame]:
    """Installed state for one prefix, with host paths resolved."""
    return read_installed(drive_c, read_product_db(drive_c), prefix=prefix)
