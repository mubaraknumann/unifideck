"""
Build display-ready GameRecord entries from owned + installed data.

``_GameBuilder`` combines:

* the UPC catalog (owned-games + metadata);
* the install registry (installed-state);
* the id_map (UPC ↔ Unifideck IDs);
* the SteamGridDB artwork URLs (if cached);

into a uniform ``GameRecord`` shape consumed by the UI. The builder
applies normalisation rules (lowercase names for sort, strip trademark
glyphs, deduplicate when UPC reports a game under multiple space_ids)
and assigns each record a stable display order.

Title cleaning/admission-filtering lives in ``title_filter.py`` and
canonical-identity/DLC dedup lives in ``identity_resolver.py`` — both
split out of this module to stay under the file-size cap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Game

from .identity_resolver import _IdentityResolver
from .title_filter import _TitleFilter, clean_launcher_title

# The Ubisoft Steam dedup filter now lives in ``.steam_filter`` and is
# applied at the Game level in ``fetch.py`` (after build), not here.

if TYPE_CHECKING:
    from unifideck.stores.ubisoft.config import UbisoftConfig
    from unifideck.stores.ubisoft.id_map import UbisoftIdMap
    from unifideck.stores.ubisoft.parser import GameConfig
logger = logging.getLogger(__name__)

class _GameBuilder:
    """Game builder."""

    def __init__(
        self,
        *,
        config: UbisoftConfig,
        id_map: UbisoftIdMap,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._id_map = id_map
        self._filters = _TitleFilter(self)
        self._identity = _IdentityResolver(self)

    @staticmethod
    def build_config_lookup(
        configs: list[GameConfig],
    ) -> dict[int, GameConfig]:
        """Build config lookup."""
        config_by_id: dict[int, GameConfig] = {}
        for cfg in configs:
            config_by_id[cfg.install_id] = cfg
            if cfg.launch_id and cfg.launch_id != cfg.install_id:
                config_by_id[cfg.launch_id] = cfg
        return config_by_id

    @staticmethod
    def cross_reference_ownership(
        configs: list[GameConfig],
        config_by_id: dict[int, GameConfig],
        owned_set: set[int] | None,
        installed: dict[str, Any] | None = None,
    ) -> list[GameConfig]:
        """Cross reference ownership.

        When the ownership binary is present (``owned_set is not None``)
        we trust it. When it's missing — which happens when no account is
        signed in, or before UPC has written the file post-login — we no
        longer return *every* config: the local binaries catalogue lists
        all configurable titles, not the ones the user owns, so that
        path invented phantom "installed" games. The fallback now keeps
        only configs that are actually installed on disk (matching the
        bootstrap-marker scan), which mirrors the ``is_installed`` test
        in :meth:`_build_one_game`.
        """
        if owned_set is not None:
            return [
                config_by_id[oid]
                for oid in owned_set
                if oid in config_by_id and config_by_id[oid].name
            ]
        installed = installed or {}
        result = [
            c for c in configs
            if c.name and _GameBuilder._config_is_installed(c, installed)
        ]
        logger.info(
            "[UbisoftLibrary] no ownership binary — keeping %d installed "
            "config entries (of %d total)",
            len(result), len([c for c in configs if c.name]),
        )
        return result

    @staticmethod
    def _config_is_installed(
        cfg: GameConfig, installed: dict[str, Any],
    ) -> bool:
        """True if ``cfg`` matches an entry in the install scan.

        Same key resolution as :meth:`_build_one_game`: a game is keyed
        by its ``space_id`` when present, otherwise by its install id.
        """
        game_id = cfg.space_id if cfg.space_id else str(cfg.install_id)
        return game_id in installed or cfg.space_id in installed

    def build_games_from_configs(
        self,
        matched_configs: list[GameConfig],
        installed: dict[str, Any],
        *,
        db_names: set[str] | None = None,
        connect_ids: dict[str, str] | None = None,
        base_catalog_norms: set[str] | None = None,
    ) -> list[Game]:
        """Two-pass build of deduped ``Game`` records from owned configs.

        ``db_names`` (normalised community game-ID DB names) widens ``" - "``
        parent detection; empty when the DB is offline. ``connect_ids`` maps
        ``space_id`` → ``ubisoftConnectGameId`` (UPC leveldb cache) and is
        recorded in the id_map so :meth:`UbisoftIdMap.resolve_launch_id`
        returns the deeplink id. ``base_catalog_norms`` (authoritative Algolia
        base-game titles) is both a keep-allowlist and the dedup identity
        anchor. See :meth:`_clean_and_filter` (pass 1) and
        :meth:`_IdentityResolver.group_by_identity` (pass 2 — canonical
        ``(base_game, edition_tag)`` grouping, then one record per group
        winner).
        """
        db_names = db_names or set()
        connect_ids = connect_ids or {}
        base_catalog_norms = base_catalog_norms or set()
        cleaned = self._clean_and_filter(matched_configs, base_catalog_norms)
        groups, order = self._identity.group_by_identity(
            cleaned, db_names, base_catalog_norms,
        )
        games: list[Game] = []
        id_map_updates: dict[str, dict[str, Any]] = {}
        for key in order:
            cfg, title = self._identity.select_group_winner(
                groups[key], connect_ids,
            )
            game = self._build_one_game(
                cfg, title, installed, id_map_updates,
            )
            if game is not None:
                games.append(game)
        if id_map_updates:
            self._id_map.update_bulk(id_map_updates)
            # Deeplink ids are reconciled after the bulk merge, not inside
            # it: the merge can only add keys, and a wrong id the cache no
            # longer supports has to be actively removed (#436).
            self._id_map.reconcile_connect_ids(
                connect_ids, list(id_map_updates),
            )
        games.sort(key=lambda g: g.title.lower())
        return games

    def _clean_and_filter(
        self,
        matched_configs: list[GameConfig],
        base_catalog_norms: set[str],
    ) -> list[tuple[GameConfig, str, bool]]:
        """Pass 1: clean titles + hard-filter, keeping ``(cfg, title,
        is_known)``.

        A catalog-known base game is kept unconditionally — the keyword
        heuristics only police entries the catalog can't vouch for.
        """
        cleaned: list[tuple[GameConfig, str, bool]] = []
        for cfg in matched_configs:
            title = clean_launcher_title(cfg.name)
            if not title:
                continue
            if self._is_third_party_steam_copy(cfg):
                logger.debug(
                    "[UbisoftLibrary] skip Steam-linked copy: %s", title,
                )
                continue
            known = self._identity.is_known_base_game(title, base_catalog_norms)
            if not known and self._filters.should_skip_launcher_title(title):
                continue
            cleaned.append((cfg, title, known))
        return cleaned

    def _is_third_party_steam_copy(self, cfg: GameConfig) -> bool:
        """True if ``cfg`` is a Steam/Epic copy that can't launch via uplay.

        UPC marks these in the config's ``third_party_platform`` block
        (e.g. ``name: Steam``). Such entitlements only launch from the
        third-party store, so their ``uplay://`` shortcut is a dead end.
        Only available on config-matched entries (backfilled synth
        configs leave the field empty). Gated by ``filter_steam_linked``
        so the user can opt out, mirroring the post-build Steam filter.
        """
        if not getattr(self._config, "filter_steam_linked", True):
            return False
        platform = (getattr(cfg, "third_party_platform", "") or "").lower()
        return platform.startswith(("steam", "epic"))

    def _build_one_game(
        self,
        cfg: GameConfig,
        title: str,
        installed: dict[str, Any],
        id_map_updates: dict[str, dict[str, Any]],
    ) -> Game | None:
        """Build one game from its canonical-group winner ``cfg``.

        ``title`` is the already-cleaned display title; dedup across the
        canonical group already happened in
        :meth:`build_games_from_configs`.
        """
        game_id = cfg.space_id if cfg.space_id else str(cfg.install_id)
        is_installed = game_id in installed or cfg.space_id in installed
        install_meta = installed.get(game_id) or installed.get(cfg.space_id) or {}
        id_map_entry: dict[str, Any] = {
            "install_id": str(cfg.install_id),
            "launch_id": str(cfg.launch_id),
            "name": title,
            "executable": getattr(cfg, "executable", None),
            "game_identifier": getattr(
                cfg,
                "game_identifier",
                None,
            ),
            "source": "local_binary",
        }
        # The leveldb-sourced connect id (the value ``uplay://launch/{id}/0``
        # expects) is applied by ``reconcile_connect_ids`` once the whole
        # batch has been merged, so precedence and stale-id removal are
        # decided in one place.
        id_map_updates[game_id] = id_map_entry
        return Game(
            app_id=0,
            store="ubisoft",
            store_game_id=game_id,
            title=title,
            installed=is_installed,
            install_path=install_meta.get("install_path"),
            exe_path=install_meta.get("executable"),
            metadata={"ownership_type": "owned"},
        )
