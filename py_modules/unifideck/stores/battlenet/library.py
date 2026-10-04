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

from .install_state import index_by_uid, normalize_uid, variant_install_rows
from .ownership import (
    AccountFacts,
    InstalledGame,
    MergedCatalog,
    evaluate_catalog,
    read_catalog,
    read_licences,
)
from .ownership.pub_catalog import CatalogEntry

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


def _version_family_and_client_selects(
    entry: CatalogEntry | None,
    installed: InstalledGame | None,
    versions_installed: int,
) -> tuple[str | None, bool]:
    """``(version_family, client_selects)`` for :func:`_game_from`.

    Split out purely to keep that function's own branching within the
    repo's complexity budget — the reasoning is unchanged from before
    the split:

    A version other than the program's own needs its own family code, and
    the client will not auto-launch it: measured, 'launch WoW' sets the
    client to WoW_retail (Install, over a finished Classic install) while
    'launch WoWC' selects WoW_wow_classic_era and waits for the user's
    Play. Retail keeps the proven auto-launching path.

    Several versions on disk is the other case the user must resolve: the
    client shows the version picker, so we select and let them press Play.
    """
    version_family = entry.launch_family_for(installed.uid) if entry and installed else None
    client_selects = bool(version_family) or versions_installed > 1
    return version_family, client_selects


def _game_metadata(
    program: str,
    entry: CatalogEntry | None,
    installed: InstalledGame | None,
    *,
    version_family: str | None,
    client_selects: bool,
    presumed: bool,
) -> dict[str, Any]:
    """The ``Game.metadata`` dict for :func:`_game_from` — split out
    purely to keep that function's own branching within the repo's
    complexity budget; field meanings are documented where they were
    before the split."""
    return {
        "family": version_family or program,
        # True when the client will only *select* this version and the
        # user presses Play there, so the launcher must not report a
        # failure when no game process appears.
        "client_selects": client_selects,
        # Diagnostic only, deliberately not a tag: tags render as pills
        # in the UI, and "presumed" is our bookkeeping, not the user's.
        "ownership": "presumed" if presumed else "granted",
        # Which version of this title is on disk, when it is not the
        # tile's own uid — Classic Era under the World of Warcraft tile.
        "installed_uid": installed.uid if installed else None,
        "title_id": entry.title_id if entry else None,
        "version": installed.version if installed else None,
        "last_played_ms": installed.last_played_ms if installed else None,
    }


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
    versions_installed: int = 0,
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
    version_family, client_selects = _version_family_and_client_selects(
        entry, installed, versions_installed,
    )
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
        metadata=_game_metadata(
            program, entry, installed,
            version_family=version_family,
            client_selects=client_selects,
            presumed=presumed,
        ),
    )


def family_updates(games: list[Game]) -> dict[str, dict[str, Any]]:
    """``uid -> {"family": …, "client_selects": …}`` per game the catalog knew.

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
            updates[game.store_game_id] = {
                "family": family,
                # Recorded alongside, because the launcher reads only the id
                # map: which family is right and whether it auto-launches are
                # the same fact about the installed version.
                "client_selects": bool(game.metadata.get("client_selects")),
            }
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
    by_uid = index_by_uid(installed)
    games = _granted_games(granted, catalog, by_uid, launcher_path, presumed=presumed)
    seen = {normalize_uid(g.store_game_id) for g in games}
    # A tile that matched a variant has consumed that install; without this
    # the orphan sweep re-adds Classic Era as a second World of Warcraft.
    seen |= {
        normalize_uid(str(g.metadata["installed_uid"]))
        for g in games if g.metadata and g.metadata.get("installed_uid")
    }
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
    candidates = entry.known_uids() if entry else ((uid,) if uid else ())
    rows = variant_install_rows(by_uid, candidates)
    return _game_from(
        program,
        entry,
        catalog,
        max(rows, key=lambda r: (r.is_ready, r.last_played_ms or 0)) if rows else None,
        versions_installed=sum(1 for row in rows if row.is_ready),
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
