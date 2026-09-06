"""
UPC ID ↔ Unifideck install ID mapping — persistent on-disk store.

UPC identifies a game by its ``space_id`` (a GUID-like string), but
Unifideck uses a stable ``install_id`` for shortcuts, save-management,
and cross-store correlation. ``UbisoftIdMap`` is the bidirectional
lookup table between the two.

It's persisted as JSON at ``UbisoftConfig.id_map_file_expanded`` and
written atomically (temp-file + ``os.replace``) so a crash during save
can't leave the table in a partial state. Reads are eager (loaded once
at construction) and writes flush after every mutation.

The class also resolves *partial* IDs (e.g. when only the install path
is known) by walking the local install directory and looking for
``goggame-style`` markers or extracted .info files.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .config import UbisoftConfig
from .id_map_sources import (
    _IdMapSources,
)
from .id_map_sources import (
    extract_game_id_from_registry as _extract_game_id_from_registry,
)
from .leveldb_ids import drop_conflicting_ids
from .leveldb_ids import (
    extract_cache_game_ids as _extract_cache_game_ids,
)
from .paths import UbisoftPrefixPaths

logger = logging.getLogger(__name__)

CONNECT_ID_KEY = "ubisoftconnect_game_id"
CONNECT_ID_SOURCE_KEY = "ubisoftconnect_game_id_source"
# Confidence order for whoever wrote a game's deeplink id, lowest first.
# A source may only overwrite an id written by an equal-or-weaker one.
# ``manual`` is a hand-edited ``ubisoft_id_map.json`` and always wins;
# ``registry`` is the installed game's own Wine registry key, which is
# per-game and therefore unambiguous; ``leveldb`` is UPC's cache. An
# entry with no source key predates #436 and is treated as untrusted.
_CONNECT_ID_SOURCES: tuple[str, ...] = (
    "",
    "name_db",
    "configurations",
    "manifest",
    "leveldb",
    "registry",
    "manual",
)

def _connect_id_rank(source: str) -> int:
    """Confidence rank of ``source``; unknown sources rank lowest."""
    if source in _CONNECT_ID_SOURCES:
        return _CONNECT_ID_SOURCES.index(source)
    return 0

class UbisoftIdMap:
    """Ubisoft ID map."""

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths
        self._cache: dict[str, dict[str, Any]] = {}
        self._load()
        self._sources = _IdMapSources(self)

    def _load(self) -> None:
        """Load."""
        path = self._config.id_map_file_expanded
        if not Path(path).is_file():
            return
        try:
            with Path(path).open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = data
                logger.info(
                    "[UbisoftIdMap] loaded %d entries from cache",
                    len(self._cache),
                )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[UbisoftIdMap] could not load cache: %s",
                e,
            )
            self._cache = {}

    def _save(self) -> None:
        """Save."""
        path = self._config.id_map_file_expanded
        try:
            Path(self._config.data_dir_expanded).mkdir(parents=True, exist_ok=True,
            )
            tmp_path = path + ".tmp"
            with Path(tmp_path).open("w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
                Path(tmp_path).replace(path)
        except OSError as e:
            logger.warning(
                "[UbisoftIdMap] could not save cache: %s",
                e,
            )

    def resolve_install_id(
        self,
        space_id: str,
    ) -> str | None:
        """Resolve install ID."""
        entry = self._cache.get(space_id, {})
        if "ubisoftconnect_game_id" in entry:
            return entry.get("ubisoftconnect_game_id")
        return entry.get("install_id")

    def resolve_launch_id(
        self,
        space_id: str,
    ) -> str | None:
        """Resolve launch ID."""
        entry = self._cache.get(space_id, {})
        if "ubisoftconnect_game_id" in entry:
            return entry.get("ubisoftconnect_game_id")
        return entry.get("launch_id")

    def update(
        self,
        space_id: str,
        install_id: str,
        launch_id: str,
    ) -> None:
        """Update."""
        self._cache[space_id] = {
            "install_id": install_id,
            "launch_id": launch_id,
        }
        self._save()

    def update_bulk(
        self,
        mapping: dict[str, dict[str, Any]],
    ) -> None:
        """Update bulk."""
        changed = False
        for space_id, entry in mapping.items():
            existing = self._cache.get(space_id, {})
            merged = {**existing, **entry}
            if merged != existing:
                self._cache[space_id] = merged
                changed = True
                if changed:
                    self._save()

    def merge_entry(
        self,
        space_id: str,
        fields: dict[str, Any],
    ) -> bool:
        """Merge entry."""
        existing = self._cache.get(space_id, {})
        merged = {**existing, **fields}
        if merged == existing:
            return False
        self._cache[space_id] = merged
        self._save()
        return True

    def set_connect_id(
        self,
        space_id: str,
        connect_id: str | None,
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Write a deeplink id only when ``source`` is trusted enough.

        The single gate for ``ubisoftconnect_game_id`` (#436). A source
        may overwrite an id recorded by an equal-or-weaker source only
        (see ``_CONNECT_ID_SOURCES``), so a leveldb re-scan during a
        force sync can no longer clobber the registry id of an installed
        game, nor a hand-edited entry. ``extra`` fields are merged either
        way. Returns whether the entry changed.
        """
        fields: dict[str, Any] = dict(extra or {})
        existing = self._cache.get(space_id, {})
        allowed = bool(connect_id) and _connect_id_rank(source) >= _connect_id_rank(
            str(existing.get(CONNECT_ID_SOURCE_KEY, "")),
        )
        if allowed:
            fields[CONNECT_ID_KEY] = str(connect_id)
            fields[CONNECT_ID_SOURCE_KEY] = source
        elif connect_id and connect_id != str(existing.get(CONNECT_ID_KEY, "")):
            logger.info(
                "[UbisoftIdMap] keeping %s id %s over %s id %s for %s",
                existing.get(CONNECT_ID_SOURCE_KEY, ""),
                existing.get(CONNECT_ID_KEY),
                source,
                connect_id,
                space_id,
            )
        return self.merge_entry(space_id, fields) if fields else False

    def reconcile_connect_ids(
        self,
        fresh: dict[str, str],
        space_ids: list[str],
    ) -> None:
        """Fold a whole leveldb scan into the map in one write.

        For every game rebuilt this sync: adopt the scanned id when
        ``leveldb`` outranks whatever is recorded, and drop a previously
        leveldb-sourced id the scan no longer corroborates —
        ``update_bulk`` merges, so a stale id would otherwise outlive
        every future sync (#436). An untagged id (written before #436)
        is left in place unless it is adopted over or the conflict sweep
        finds it shared; on its own it is not evidence of damage.
        """
        rank = _connect_id_rank("leveldb")
        changed = False
        for space_id in space_ids:
            entry = self._cache.setdefault(space_id, {})
            connect_id = fresh.get(space_id)
            current_rank = _connect_id_rank(str(entry.get(CONNECT_ID_SOURCE_KEY, "")))
            if current_rank > rank:
                continue
            if connect_id:
                changed |= entry.get(CONNECT_ID_KEY) != connect_id
                entry[CONNECT_ID_KEY] = connect_id
                entry[CONNECT_ID_SOURCE_KEY] = "leveldb"
            elif entry.get(CONNECT_ID_SOURCE_KEY) == "leveldb":
                entry.pop(CONNECT_ID_KEY, None)
                entry.pop(CONNECT_ID_SOURCE_KEY, None)
                changed = True
                logger.info(
                    "[UbisoftIdMap] dropped uncorroborated deeplink id for %s",
                    space_id,
                )
        if changed:
            self._save()

    def sweep_conflicting_connect_ids(self) -> int:
        """Repair a persisted map where one deeplink id serves two games.

        Self-heal for users already carrying the #436 damage: an id is
        kept only on an entry that corroborates it (its own
        ``install_id``/``launch_id``, or a registry/manual source) and
        stripped from every other claimant, which then re-derives its id
        on this same sync. Returns the number of entries stripped.
        """
        owners: dict[str, list[str]] = {}
        for space_id, entry in self._cache.items():
            connect_id = entry.get(CONNECT_ID_KEY)
            if connect_id:
                owners.setdefault(str(connect_id), []).append(space_id)
        stripped = 0
        for connect_id, spaces in owners.items():
            if len(spaces) > 1:
                stripped += self._resolve_connect_id_conflict(connect_id, spaces)
        if stripped:
            self._save()
        return stripped

    def _resolve_connect_id_conflict(
        self,
        connect_id: str,
        spaces: list[str],
    ) -> int:
        """Strip ``connect_id`` from every space id that can't corroborate it."""
        keeper = next(
            (s for s in sorted(spaces) if self._corroborates_connect_id(s, connect_id)),
            None,
        )
        logger.warning(
            "[UbisoftIdMap] deeplink id %s claimed by %d games (%s) — keeping it "
            "on %s",
            connect_id,
            len(spaces),
            ", ".join(sorted(spaces)),
            keeper or "none",
        )
        stripped = 0
        for space_id in spaces:
            if space_id == keeper:
                continue
            entry = self._cache[space_id]
            entry.pop(CONNECT_ID_KEY, None)
            entry.pop(CONNECT_ID_SOURCE_KEY, None)
            stripped += 1
        return stripped

    def _corroborates_connect_id(self, space_id: str, connect_id: str) -> bool:
        """Whether ``space_id`` has independent evidence for ``connect_id``."""
        entry = self._cache.get(space_id, {})
        if entry.get(CONNECT_ID_SOURCE_KEY) in ("registry", "manual"):
            return True
        return connect_id in (
            str(entry.get("install_id", "")),
            str(entry.get("launch_id", "")),
        )

    def get_entry(
        self,
        space_id: str,
    ) -> dict[str, Any]:
        """Get entry."""
        return dict(self._cache.get(space_id, {}))

    def in_cache(self, space_id: str) -> bool:
        """In cache."""
        return space_id in self._cache

    def find_cached_entry_by_install_id(
        self,
        install_id: int | str,
    ) -> dict[str, Any] | None:
        """Reverse-lookup a previously-cached entry by its ``install_id``.

        Fallback for when an owned ``install_id`` can't be named by the
        community game-ID DB (see ``_LibraryFetcher._build_backfill_configs``):
        if Unifideck has already identified this exact game in a prior
        session — most commonly via local-binary detection, which finds
        installed games the community DB has never heard of — reuse that
        cached identity (name, ``space_id``, executable) instead of
        dropping the game from the library. Returns ``None`` when no
        cached entry has a matching, named ``install_id``.
        """
        target = str(install_id)
        for space_id, entry in self._cache.items():
            if str(entry.get("install_id", "")) == target and entry.get("name"):
                result = dict(entry)
                result["space_id"] = space_id
                return result
        return None

    def resolve_prefix_path(self, space_id: str) -> str | None:
        """Recorded absolute Wine-prefix path for a game, if any.

        Ubisoft games can be installed to a user-picked storage location
        (internal / SD / custom); the per-game prefix is created there and
        its absolute path recorded here so every store-side consumer — and
        the separate launcher process, which reads this same JSON — resolves
        the identical directory. Returns ``None`` for games on the fixed
        internal default (back-compat).
        """
        path = self._cache.get(space_id, {}).get("prefix_path")
        return path if isinstance(path, str) and path else None

    def set_prefix_path(self, space_id: str, prefix_path: str) -> None:
        """Record the absolute Wine-prefix path for a game."""
        self.merge_entry(space_id, {"prefix_path": prefix_path})

    def clear_prefix_path(self, space_id: str) -> None:
        """Drop the recorded prefix path (e.g. after an abandoned install)."""
        entry = self._cache.get(space_id)
        if entry and "prefix_path" in entry:
            entry.pop("prefix_path", None)
            self._save()

    def all_prefix_paths(self) -> list[str]:
        """All recorded per-game prefix paths (deduped, order-preserving)."""
        seen: set[str] = set()
        out: list[str] = []
        for entry in self._cache.values():
            path = entry.get("prefix_path")
            if isinstance(path, str) and path and path not in seen:
                seen.add(path)
                out.append(path)
        return out

    def iter_all_game_prefix_paths(self) -> list[str]:
        """Union of the fixed-base scan and recorded external prefixes.

        Per-game prefixes can live outside ``prefixes_dir`` (SD / custom),
        so consumers that enumerate prefixes (install detection, session
        propagation, id-map refresh) must union both. Delegated to
        :class:`UbisoftPrefixPaths`, which owns prefix-location logic.
        """
        return self._paths.iter_all_game_prefix_paths()

    async def refresh_from_configurations(
        self,
        space_id: str | None = None,
    ) -> bool:
        """Refresh from configurations."""
        return await self._sources.refresh_from_configurations(
            space_id,
        )

    async def fetch_game_id_database(
        self,
        force: bool = False,
    ) -> list[tuple[str, str]]:
        """Fetch game ID database (``force`` bypasses the TTL cache)."""
        return await self._sources.fetch_game_id_database(force=force)

    async def fetch_uuid_catalog(
        self,
        force: bool = False,
    ) -> dict[str, str]:
        """``uuid → name`` from unifiDB (``force`` bypasses the TTL cache)."""
        return await self._sources.fetch_uuid_catalog(force=force)

    async def lookup_game_id_by_name(
        self,
        game_name: str,
    ) -> str | None:
        """Lookup game ID by name."""
        return await self._sources.lookup_game_id_by_name(
            game_name,
        )

    @staticmethod
    def extract_game_id_from_registry(
        prefix_path: str,
    ) -> str | None:
        """Extract game ID from registry."""
        return _extract_game_id_from_registry(prefix_path)

    def read_connect_ids(self) -> dict[str, str]:
        """``space_id`` → ``ubisoftConnectGameId`` from UPC's leveldb cache.

        Scans the auth prefix first, then the template — the first that
        yields any mappings wins. Read-only: the caller folds the result
        into the id_map (the builder records it on each game so
        :meth:`resolve_launch_id` returns the canonical deeplink id).
        Returns an empty dict when no cache is present.

        An id claimed by more than one game is dropped rather than
        guessed at (#436); that game falls back to its configurations
        ``launch_id``, exactly as it would with no cache at all.
        """
        for prefix_dir in (
            self._config.auth_prefix_dir_expanded,
            self._config.template_dir_expanded,
        ):
            ids = _extract_cache_game_ids(
                prefix_dir,
                self._config.localstorage_relative_path,
            )
            if ids:
                return drop_conflicting_ids(ids)
        return {}

    @staticmethod
    def _normalize_for_matching(name: str) -> str:
        """Normalize for matching."""
        name = name.lower()
        name = name.replace("_", " ")
        name = re.sub(
            r"[®™©''\u2019\-:.,!?()\"']",
            "",
            name,
        )
        return " ".join(name.split())

    def normalize_for_matching(self, name: str) -> str:
        """Normalize for matching."""
        return self._normalize_for_matching(name)
