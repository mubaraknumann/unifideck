"""Library-cache persistence mixin for :class:`SyncService`.

Extracted from ``core/sync_service.py`` to keep that file under the
550-LOC volumetry cap. Owns the on-disk ``library_cache.json`` round
trip — loaded once at construction, saved after every finalize and
install-state flip — so a Decky reload restarts with the last synced
library instead of an empty one.

Declares its consumed attributes (``_config``, ``_all_games``,
``_last_sync_time``) as ``TYPE_CHECKING`` annotations only; the host
``SyncService`` provides them at runtime, the same convention the
other sync mixins use.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .types import Game

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

class _SyncCacheMixin:
    """``library_cache.json`` load/save for :class:`SyncService`."""

    # Provided by the host SyncService at runtime.
    _config: ConfigManager | None
    _all_games: dict[str, list[Game]]
    _last_sync_time: float | None
    _bus: Any
    _store_sync_times: dict[str, float]

    def _get_library_cache_path(self) -> Path:
        """Resolve the library_cache.json file path."""
        from unifideck.utils.paths import get_games_map_path
        map_path = get_games_map_path(self._config)
        return Path(map_path).parent / "library_cache.json"

    def _load_library_cache(self) -> None:
        """Load synced libraries from disk cache."""
        try:
            cache_path = self._get_library_cache_path()
            if not cache_path.is_file():
                return

            from unifideck.config.config_persistence import load_json_layer
            data = load_json_layer(cache_path)
            if not data:
                return

            last_sync = data.get("last_sync_time")
            if isinstance(last_sync, (int, float)):
                self._last_sync_time = float(last_sync)
            self._store_sync_times = _deserialize_sync_times(
                data.get("store_sync_times"),
            )

            libraries_data = data.get("libraries", {})
            if not isinstance(libraries_data, dict):
                return

            self._all_games = _deserialize_libraries(libraries_data)
            logger.info(
                "[SyncService] Loaded %d cached games from library_cache.json",
                sum(len(g) for g in self._all_games.values()),
            )
            self._annotate_loaded_cache()
        except Exception as e:
            logger.warning("[SyncService] Failed to load library cache: %s", e)

    def _annotate_loaded_cache(self) -> None:
        """Re-stamp the duplicate-grouping fields on the in-memory library.

        Runs on cache load (a cache from an older build, or from before
        the user's last sync, carries stale fields), after the metadata
        phase, and when the frontend pushes the owned Steam library.

        Best-effort: a failure here must not stop the plugin starting with
        its cached library; grouping just stays as it was until the next
        sync.
        """
        try:
            from unifideck.core.game_grouping import (
                annotate_duplicate_groups_if_enabled,
            )

            # One call over every store together: grouping is cross-store,
            # so an Epic copy has to see its GOG sibling. The Game objects
            # are mutated in place, the same ones held in `_all_games`.
            all_games = [g for games in self._all_games.values() for g in games]
            annotate_duplicate_groups_if_enabled(
                all_games, self._config, getattr(self, "_cache", None),
            )
        except Exception:
            logger.exception(
                "[SyncService] failed to annotate duplicate groups on "
                "cache load — continuing without cross-store grouping "
                "until the next sync",
            )

    def _subscribe_grouping_refresh(self) -> None:
        """Re-group when new Steam mappings land after a sync.

        Grouping reads ``steam_real_appid``, which the metadata phase
        writes *after* ``sync_complete`` and the metadata backfill writes
        later still. Without this, a newly synced game would group by
        title alone until the next sync.
        """
        from unifideck.core.types import Events

        self._bus.on(Events.POST_SYNC_PHASE_CHANGED, self._on_metadata_phase_done)
        self._bus.on(Events.METADATA_BACKFILL_COMPLETE, self._on_metadata_backfilled)

    def _on_metadata_phase_done(self, **kwargs: Any) -> None:
        if kwargs.get("phase") == "metadata" and not kwargs.get("active", True):
            self.refresh_duplicate_groups()

    def _on_metadata_backfilled(self, **_kwargs: Any) -> None:
        self.refresh_duplicate_groups()

    def refresh_duplicate_groups(self) -> None:
        """Re-group the in-memory library and persist it."""
        self._annotate_loaded_cache()
        self._save_library_cache()

    def reset_library_state(self) -> None:
        """Drop the in-memory library and its on-disk cache.

        The exact inverse of :meth:`_load_library_cache`, and the reason
        it exists: "Delete all Unifideck data" removes
        ``library_cache.json`` from disk in *both* modes, but the process
        keeps serving ``_all_games`` — so the Downloads tab kept listing
        games whose files had just been deleted, and the next
        :meth:`_save_library_cache` (fired by any finalize or
        install-state flip) wrote the wiped library straight back.

        Memory is cleared *before* the file so a concurrent save can only
        ever persist the empty state, never resurrect the old one.
        Unlinking here as well as in the data-dir sweep keeps the method
        correct on its own, whatever order callers use.
        """
        self._all_games = {}
        self._last_sync_time = None
        self._store_sync_times = {}
        with contextlib.suppress(OSError):
            self._get_library_cache_path().unlink(missing_ok=True)
        logger.info("[SyncService] library state reset (in-memory + cache file)")

    def _save_library_cache(self) -> None:
        """Save current unified library state to disk cache."""
        try:
            cache_path = self._get_library_cache_path()
            from dataclasses import asdict

            libraries_data = {}
            for store_name, games in self._all_games.items():
                libraries_data[store_name] = [asdict(g) for g in games]

            payload = {
                "last_sync_time": self._last_sync_time,
                "store_sync_times": dict(self._store_sync_times),
                "libraries": libraries_data,
            }

            from unifideck.config.config_persistence import atomic_write_json
            atomic_write_json(cache_path, payload)
            logger.info(
                "[SyncService] Saved library cache (%d games) to "
                "library_cache.json",
                sum(len(g) for g in self._all_games.values()),
            )
        except Exception as e:
            logger.warning("[SyncService] Failed to save library cache: %s", e)

def _deserialize_libraries(
    libraries_data: dict[str, Any],
) -> dict[str, list[Game]]:
    """Rebuild ``{store: [Game]}`` from the cached JSON dicts.

    Unknown keys are dropped so a cache written by a newer build (with
    extra ``Game`` fields) still loads on an older one.
    """
    from dataclasses import fields

    game_fields = {f.name for f in fields(Game)}
    loaded: dict[str, list[Game]] = {}
    for store_name, game_dicts in libraries_data.items():
        if not isinstance(game_dicts, list):
            continue
        games_list = [
            Game(**{k: v for k, v in gd.items() if k in game_fields})
            for gd in game_dicts
            if isinstance(gd, dict)
        ]
        loaded[store_name] = games_list
    return loaded


def _deserialize_sync_times(raw: Any) -> dict[str, float]:
    """Rebuild the per-store sync stamps; anything malformed is dropped.

    A cache written before per-store stamps existed has no key at all,
    which reads as "no stamps" — the store rows then fall back to the
    whole-library ``last_sync_time``.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        name: float(ts)
        for name, ts in raw.items()
        if isinstance(name, str)
        and isinstance(ts, (int, float))
        and not isinstance(ts, bool)
    }
