"""
Fetch the owned-games catalog from the UPC user data.

``_LibraryFetch`` reads the UPC catalog from the user's Wine prefix
(``ownership`` and ``configurations`` directories) and returns the
parsed owned-games list. Delegates to ``parser.py`` and
``parser_binary.py`` for the actual decoding.

Errors during read are surfaced as empty results — the caller will fall
back to "installed games only" mode if the owned list can't be read.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from unifideck.core.types import Game

from .data_loader import _DataLoader
from .game_builder import _GameBuilder
from .steam_filter import apply_steam_owned_filter, load_steam_owned_titles

if TYPE_CHECKING:
    from unifideck.stores.ubisoft.config import UbisoftConfig
    from unifideck.stores.ubisoft.id_map import UbisoftIdMap

    # GameConfig is used in the ``ParseConfigurationsFn`` alias just
    # below as a string forward-ref. flake8 can't see through string
    # annotations so it flags F401 — silenced explicitly.
    from unifideck.stores.ubisoft.parser import GameConfig
    from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
ParseConfigurationsFn = Callable[[str], "list[GameConfig]"]
ParseOwnershipFn = Callable[[str], list[int]]
logger = logging.getLogger(__name__)

class _PreparedLibrary(NamedTuple):
    """The owned-games working set, ready to hand to the game builder.

    Bundles the cross-referenced + backfilled configs with the name
    allowlists derived from the catalog sources, so ``fetch_local_binaries``
    stays a thin orchestrator (see :meth:`_LibraryFetcher._prepare_library`).
    """

    matched_configs: list[GameConfig]
    db_names: set[str]
    base_catalog_norms: set[str]
    id_backfill: int
    uuid_backfill: int

class _LibraryFetcher:
    """Library fetcher."""

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        id_map: UbisoftIdMap,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths
        self._id_map = id_map
        self._loader = _DataLoader(config=config, paths=paths)
        self._builder = _GameBuilder(
            config=config,
            id_map=id_map,
        )

    async def fetch_local_binaries(
        self,
        installed: dict[str, Any],
        *,
        force: bool = False,
    ) -> list[Game] | None:
        """Fetch local binaries.

        ``force`` (a force-sync) makes the unifiDB lookups (install_id list +
        uuid catalog) bypass their TTL cache and re-download.
        """
        parser_funcs = self._import_ubisoft_parser()
        if parser_funcs is None:
            return None
        parse_configurations, parse_ownership = parser_funcs
        configs = await self._loader.load_configurations(
            parse_configurations,
        )
        if not configs:
            return None
        owned_set = await self._loader.load_ownership_set(
            parse_ownership,
        )
        owned_uuids = await self._loader.load_ownership_uuids()
        if owned_set is None:
            # get_library is auth-gated upstream, so reaching here means
            # we ARE signed in but UPC hasn't written its ownership cache
            # yet (it can lag the credential capture by a few seconds, or
            # the user closed UPC before it finished syncing). We fall
            # back to installed-only (anti-phantom) and surface the state
            # so a "signed in but library looks empty" report is
            # diagnosable; the next refresh picks up the cache.
            logger.warning(
                "[UbisoftLibrary] authenticated but UPC ownership cache "
                "absent — UPC may still be syncing; showing installed-only "
                "until the next library refresh",
            )
        prepared = await self._prepare_library(
            configs, owned_set, owned_uuids, installed, force=force,
        )
        # Repair a map poisoned before #436 was fixed (one deeplink id
        # shared by two games) before the rebuild re-derives the ids.
        await asyncio.to_thread(self._id_map.sweep_conflicting_connect_ids)
        connect_ids = await asyncio.to_thread(self._id_map.read_connect_ids)
        games = self._builder.build_games_from_configs(
            prepared.matched_configs,
            installed,
            db_names=prepared.db_names,
            connect_ids=connect_ids,
            base_catalog_norms=prepared.base_catalog_norms,
        )
        games = await self._apply_steam_filter(games)
        logger.info(
            "[UbisoftLibrary] local binary library: %d games "
            "(%d config-matched + %d id-backfilled + %d uuid-backfilled)",
            len(games),
            len(prepared.matched_configs)
            - prepared.id_backfill
            - prepared.uuid_backfill,
            prepared.id_backfill,
            prepared.uuid_backfill,
        )
        return games

    async def _prepare_library(
        self,
        configs: list[GameConfig],
        owned_set: set[int] | None,
        owned_uuids: set[str],
        installed: dict[str, Any],
        *,
        force: bool,
    ) -> _PreparedLibrary:
        """Cross-reference, backfill, and derive name allowlists.

        Combines the owned∩config match with the two backfill sources and
        the catalog-derived name sets into a single working set so the public
        fetch entry point stays a thin orchestrator.
        """
        config_by_id = self._builder.build_config_lookup(configs)
        matched_configs = self._builder.cross_reference_ownership(
            configs,
            config_by_id,
            owned_set,
            installed,
        )
        db_entries, uuid_catalog = await self._load_catalog_data(force=force)
        db_names, base_catalog_norms = self._build_name_sets(
            db_entries, uuid_catalog,
        )
        matched_configs, id_backfill, uuid_backfill = self._apply_backfills(
            matched_configs,
            owned_set,
            owned_uuids,
            config_by_id,
            configs,
            db_entries,
            uuid_catalog,
        )
        return _PreparedLibrary(
            matched_configs=matched_configs,
            db_names=db_names,
            base_catalog_norms=base_catalog_norms,
            id_backfill=id_backfill,
            uuid_backfill=uuid_backfill,
        )

    async def _load_catalog_data(
        self,
        *,
        force: bool,
    ) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """The two unifiDB catalog sources: the legacy install-id ``(id,
        name)`` list and the Algolia base-game uuid→name catalog. ``force``
        bypasses both TTL caches."""
        db_entries = await self._fetch_db_entries(force=force)
        uuid_catalog = await self._id_map.fetch_uuid_catalog(force=force)
        return db_entries, uuid_catalog

    def _build_name_sets(
        self,
        db_entries: list[tuple[str, str]],
        uuid_catalog: dict[str, str],
    ) -> tuple[set[str], set[str]]:
        """Derive the two name allowlists from the catalog sources.

        The Algolia uuid catalog is base-games-only (no DLC/noise), so its
        names are the authoritative allowlist + identity anchor for dedup —
        returned separately as ``base_catalog_norms``. ``db_names`` is the
        union with the legacy install-id list (which is polluted with
        DLC/edition/QC rows), used for DLC parent-name detection.
        """
        base_catalog_norms = {
            self._id_map.normalize_for_matching(name)
            for name in uuid_catalog.values()
            if name
        }
        db_names = {
            self._id_map.normalize_for_matching(name)
            for _iid, name in db_entries
            if name
        } | base_catalog_norms
        return db_names, base_catalog_norms

    def _apply_backfills(
        self,
        matched_configs: list[GameConfig],
        owned_set: set[int] | None,
        owned_uuids: set[str],
        config_by_id: dict[int, GameConfig],
        configs: list[GameConfig],
        db_entries: list[tuple[str, str]],
        uuid_catalog: dict[str, str],
    ) -> tuple[list[GameConfig], int, int]:
        """Append synthesized configs for owned games with no local
        ``configurations`` row.

        UPC only caches configs for installed/recent titles, so the
        owned∩config intersection is tiny (~6 of 118 owned IDs here). Two
        complementary sources name the rest so the full owned library shows:
        the legacy install_id list (numeric ids) and the unifiDB uuid catalog
        (modern ids the legacy list lacks). Both flow through the same
        dedup/DLC filters in ``build_games_from_configs``. Returns the
        extended list plus the per-source backfill counts (for the summary
        log).
        """
        id_backfill = 0
        uuid_backfill = 0
        if owned_set is not None:
            extra = self._build_backfill_configs(
                owned_set, config_by_id, configs, db_entries,
            )
            id_backfill = len(extra)
            matched_configs = matched_configs + extra
        if owned_uuids and uuid_catalog:
            extra_uuid = self._build_uuid_backfill_configs(
                owned_uuids, uuid_catalog,
            )
            uuid_backfill = len(extra_uuid)
            matched_configs = matched_configs + extra_uuid
        return matched_configs, id_backfill, uuid_backfill

    async def _apply_steam_filter(
        self,
        games: list[Game],
    ) -> list[Game]:
        """Hide games already owned on Steam (when enabled).

        Gated by ``filter_steam_linked``; the (blocking) Steam library
        scan runs off the event loop. A Steam-owned Ubisoft title can't
        launch via ``uplay://`` so its shortcut would be a dead end —
        see :mod:`.steam_filter`.
        """
        if not self._config.filter_steam_linked:
            return games
        steam_titles = await asyncio.to_thread(load_steam_owned_titles)
        filtered, _hidden = apply_steam_owned_filter(games, steam_titles)
        return filtered

    async def _fetch_db_entries(
        self,
        *,
        force: bool = False,
    ) -> list[tuple[str, str]]:
        """Community game-ID DB as ``(install_id, name)`` pairs.

        Feeds both DLC parent-name detection and the owned-game backfill.
        ``force`` bypasses the TTL cache. Degrades to an empty list when the
        database is offline or unavailable.
        """
        try:
            return await self._id_map.fetch_game_id_database(force=force)
        except Exception:
            logger.debug(
                "[UbisoftLibrary] game-ID DB unavailable",
            )
            return []

    def _build_uuid_backfill_configs(
        self,
        owned_uuids: set[str],
        uuid_catalog: dict[str, str],
    ) -> list[GameConfig]:
        """Synthesize ``GameConfig`` rows for owned product UUIDs, named via
        the unifiDB uuid catalog. These cover modern games whose install_ids
        the legacy list lacks. The UUID becomes the game's ``space_id`` (and
        thus its ``store_game_id``), so leveldb-sourced ``connect_ids`` can
        still supply a ``uplay://`` deeplink. Names colliding with an
        install_id-backfilled game collapse via the dedup in
        ``build_games_from_configs``.
        """
        from unifideck.stores.ubisoft.parser import GameConfig

        backfilled: list[GameConfig] = []
        unresolved: list[str] = []
        for uuid in owned_uuids:
            name = uuid_catalog.get(uuid)
            if not name:
                unresolved.append(uuid)
                continue
            synth = GameConfig()
            synth.space_id = uuid
            synth.name = name
            backfilled.append(synth)
        if unresolved:
            logger.info(
                "[UbisoftLibrary] %d owned UUID(s) unnamed by uuid catalog "
                "(dropped): %s",
                len(unresolved),
                ", ".join(sorted(unresolved)[:20]),
            )
        return backfilled

    def _build_backfill_configs(
        self,
        owned_set: set[int],
        config_by_id: dict[int, GameConfig],
        configs: list[GameConfig],
        db_entries: list[tuple[str, str]],
    ) -> list[GameConfig]:
        """Synthesize ``GameConfig`` rows for owned IDs absent from the local
        ``configurations`` cache: named via the community DB, a parsed
        config name, or (last resort) Unifideck's own previously-cached
        id_map entry — see :meth:`_resolve_backfill_identity`. Only IDs
        unnamed by every source are skipped, staying unlisted until one
        covers them. Edition/DLC/test noise is removed downstream by
        :meth:`_GameBuilder.build_games_from_configs`.
        """
        id_to_name: dict[int, str] = {}
        for iid_str, name in db_entries:
            if not name:
                continue
            try:
                iid = int(iid_str)
            except (TypeError, ValueError):
                continue
            id_to_name.setdefault(iid, name)
        for cfg in configs:
            if cfg.name:
                id_to_name.setdefault(cfg.install_id, cfg.name)

        backfilled: list[GameConfig] = []
        unresolved: list[int] = []
        recovered_from_cache = 0
        for oid in owned_set:
            if oid in config_by_id:
                continue
            synth, from_cache = self._resolve_backfill_identity(
                oid, id_to_name,
            )
            if synth is None:
                unresolved.append(oid)
                continue
            backfilled.append(synth)
            recovered_from_cache += from_cache
        if unresolved:
            logger.info(
                "[UbisoftLibrary] %d owned install_id(s) unnamed by "
                "community DB (dropped): %s",
                len(unresolved),
                ", ".join(str(i) for i in sorted(unresolved)[:20]),
            )
        if recovered_from_cache:
            logger.info(
                "[UbisoftLibrary] %d owned install_id(s) recovered from "
                "Unifideck's own cache (community DB had no name)",
                recovered_from_cache,
            )
        return backfilled

    def _resolve_backfill_identity(
        self,
        oid: int,
        id_to_name: dict[int, str],
    ) -> tuple[GameConfig | None, bool]:
        """Resolve one owned, unconfigured install_id to a synthesized
        ``GameConfig``, falling back to a cached id_map entry (see
        :meth:`UbisoftIdMap.find_cached_entry_by_install_id`) when
        neither the community DB nor a parsed config names it.

        The cache fallback matters because the community DB is
        crowd-sourced and best-effort — it can simply never have
        catalogued an older/less common title (e.g. one superseded by
        an HD remaster under a different install_id) that Unifideck
        itself already correctly identified in an earlier session,
        typically via local-binary detection. Reusing that cached
        identity wholesale (not just its name) carries the correct
        ``space_id`` through to install-status and prefix resolution
        downstream (see ``_build_one_game``'s ``game_id``).

        Returns ``(config_or_None, recovered_from_cache)``.
        """
        from unifideck.stores.ubisoft.parser import GameConfig

        resolved = id_to_name.get(oid)
        cached = (
            None if resolved
            else self._id_map.find_cached_entry_by_install_id(oid)
        )
        if not resolved and not cached:
            return None, False

        synth = GameConfig()
        synth.install_id = oid
        synth.launch_id = oid
        if cached:
            synth.name = cached.get("name") or ""
            synth.space_id = cached.get("space_id") or ""
            synth.executable = cached.get("executable") or ""
            synth.game_identifier = cached.get("game_identifier") or ""
            return synth, True
        synth.name = resolved or ""
        return synth, False

    @staticmethod
    def _import_ubisoft_parser() -> (
        tuple[ParseConfigurationsFn, ParseOwnershipFn] | None
    ):
        """Import UBISOFT parser."""
        try:
            from unifideck.stores.ubisoft.parser import (
                parse_configurations,
                parse_ownership,
            )
        except ImportError:
            logger.exception("[UbisoftLibrary] ubisoft_parser unavailable")
            return None
        return parse_configurations, parse_ownership
