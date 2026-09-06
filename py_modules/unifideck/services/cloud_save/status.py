"""services/cloud_save/status.py — save-dir resolution + status surface.

Mixin split out of ``service.py`` (which had crossed the 550-LOC complexity
gate). Groups the read-only "where do this game's saves live, and what is
their cloud status" concern that backs the manual cloud-save button, kept
separate from the reactive sync orchestration in ``service.py``/``sync.py``.

This is a mixin: ``self`` is the ``CloudSaveService`` facade at runtime. The
shared instance attributes it reads are declared (not assigned) below so the
type checker resolves them without falling back to ``Any``.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_drive_c
from unifideck.services.cloud_save.path_resolver import find_save_dir_by_title

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)


class _StatusMixin:
    """Save-location resolution and manual-button status reporting."""

    # Provided by the concrete CloudSaveService (assigned in its __init__).
    _local_root: str
    _cloud_root: str | None
    _config: ConfigManager | None
    _cache: Any
    _strategies: dict[str, Any]
    _syncing: dict[str, asyncio.Lock]

    def _auto_enabled(self, key: str, *, default: bool) -> bool:
        """Read a ``cloud.<key>`` boolean flag, falling back to ``default``."""
        if not self._config:
            return default
        try:
            return bool(self._config.get(f"cloud.{key}", default))
        except Exception:
            return default

    def _detect_wine_prefix_save_dir(self, game_id: str) -> str | None:
        """Attempt to auto-detect common locations under the wine prefix."""
        try:
            prefix_root = Path(self._local_root).parent / "prefixes" / game_id
            drive_c = resolve_drive_c(prefix_root)
            if not drive_c:
                return None

            game_title = ""
            if self._config:
                game_title = self._config.get(f"games.{game_id}.title") or ""
            # No title ⇒ nothing to match a prefix subfolder against.
            return find_save_dir_by_title(drive_c, game_title)
        except Exception as e:
            logger.debug("[CloudSave] Failed to auto-detect save dir: %s", e)
        return None

    def get_local_save_dir(self, store: str, game_id: str) -> str | None:
        """Resolve a game's ACTUAL local save directory, or ``None``.

        Order: explicit config override → store strategy (prefix-resolved) →
        wine-prefix auto-detect. Returns ``None`` when no real location can be
        found (e.g. the game was never launched, so no prefix exists). We do
        NOT fall back to a ``saves/<store>/<id>`` staging dir — the game never
        reads from there, so syncing/backing it up only strands saves.
        """
        if self._config:
            configured = self._config.get(f"games.{game_id}.save_path")
            if configured:
                return str(configured)

        if store in self._strategies:
            strat_dir: str | None = self._strategies[store].get_local_save_dir(game_id)
            if strat_dir:
                return strat_dir

        # Try to auto-detect prefix folder (real, in-prefix location).
        return self._detect_wine_prefix_save_dir(game_id)

    def _has_save_path_override(self, game_id: str) -> bool:
        """True when ``games.<id>.save_path`` holds a manual override.

        The status surface reports this so the manual cloud-save window can
        offer "reset to automatic" only when there is an override to reset —
        auto-detected locations have nothing to revert to.
        """
        if not self._config:
            return False
        try:
            return bool(self._config.get(f"games.{game_id}.save_path"))
        except Exception:
            return False

    # ── Manual cloud-save button: status surface ─────────────────────
    def is_syncing(self, store: str, game_id: str) -> bool:
        """True if a sync for this game is currently in flight."""
        lock = self._syncing.get(f"{store}:{game_id}")
        return bool(lock and lock.locked())

    def _cloud_supported(self, store: str, game_id: str) -> bool | None:
        """Native cloud-save support for ``store``, or ``None`` if unknown.

        Shares :func:`~.support.resolve_cloud_support` with the App-Details
        panel so the button and the panel can never contradict each other:
        Epic answers from its own cached metadata, everything else from the
        unifiDB ``cloud`` map. ``None`` when neither knows — many Epic games
        genuinely lack cloud support, which is why "didn't sync" is sometimes
        "nothing to sync".
        """
        from .support import resolve_cloud_support
        meta: Any = None
        if self._cache is not None:
            try:
                meta = self._cache.get("metadata", f"{store}:{game_id}")
            except Exception:
                meta = None
        return resolve_cloud_support(store, game_id, meta)

    def _browse_start(self, game_id: str, save_dir: str | None) -> str:
        """Best starting folder for the manual save-location picker.

        Prefers the game's prefix user dir (where in-prefix saves live), then an
        existing resolved/fallback save dir, then the user's home.
        """
        users = (
            Path(self._local_root).parent / "prefixes" / game_id
            / "drive_c" / "users" / "steamuser"
        )
        if users.is_dir():
            return str(users)
        if save_dir and Path(save_dir).is_dir():
            return str(save_dir)
        return str(Path.home())

    def _base_status(
        self, store: str, game_id: str, supported: bool
    ) -> dict[str, Any]:
        """Default status dict before save-dir / cloud info is layered on."""
        return {
            "supported": supported,
            "in_progress": self.is_syncing(store, game_id),
            "auto_pull": self._auto_enabled("auto_pull_on_launch", default=True),
            "auto_push": self._auto_enabled("auto_push_on_stop", default=False),
            "cloud_supported": self._cloud_supported(store, game_id),
            "save_path": None,
            "save_path_resolved": False,
            "save_path_is_override": False,
            "has_local_saves": False,
            "local_snapshot": {},
            "has_cloud_saves": None,
            "remote_snapshot": None,
            "last_sync_ts": 0.0,
            "browse_start": str(Path.home()),
        }

    @staticmethod
    def _local_save_status(save_dir: str | None) -> dict[str, Any]:
        """Local-save fields (path + presence + snapshot) for ``save_dir``.

        ``save_dir`` is always a REAL resolved location now (or None) — the
        staging fallback is gone, so there's no stale dir to misreport as
        "local saves" (e.g. after the prefix was deleted).
        """
        from .safety import has_save_data, snapshot
        out: dict[str, Any] = {
            "save_path": None,
            "save_path_resolved": False,
            "has_local_saves": False,
            "local_snapshot": {},
        }
        if save_dir:
            out["save_path"] = save_dir
            out["save_path_resolved"] = True
            if Path(save_dir).is_dir():
                out["has_local_saves"] = has_save_data(save_dir)
                out["local_snapshot"] = snapshot(save_dir)
        return out

    @staticmethod
    def _merge_cloud_info(
        status: dict[str, Any],
        cloud_info: dict[str, Any] | None,
        remote: dict[str, Any],
    ) -> None:
        """Layer real store-cloud info over the local mirror hint.

        Prefer the REAL store-cloud timestamp/presence (e.g. legendary
        list-saves) over the local backup mirror, which can be stale and
        would make "Cloud" misleadingly differ from "Local". File count/size
        still come from the mirror as a rough hint.
        """
        if cloud_info is not None:
            has = bool(cloud_info.get("has_saves"))
            status["has_cloud_saves"] = has
            if has:
                ts = float(cloud_info.get("timestamp") or 0.0)
                # ONLY what the real cloud reported — never backfill from the
                # local mirror (a stale/wrong size is worse than no size). 0
                # means "unknown" and the UI omits that field.
                status["remote_snapshot"] = {
                    "timestamp": ts,
                    "file_count": int(cloud_info.get("file_count") or 0),
                    "total_bytes": int(cloud_info.get("total_bytes") or 0),
                }
                status["last_sync_ts"] = ts
        elif remote and remote.get("file_count"):
            status["remote_snapshot"] = remote
            status["has_cloud_saves"] = True
            status["last_sync_ts"] = float(remote.get("timestamp") or 0.0)
        elif status["cloud_supported"] is False:
            status["has_cloud_saves"] = False

    async def get_cloud_status(self, store: str, game_id: str) -> dict[str, Any]:
        """Out-of-band status for the manual cloud-save button.

        Possibly slow (``get_local_save_dir`` may hit the store's metadata on
        first call), so callers must fetch this off the render hot path.
        ``has_cloud_saves`` is best-effort: ``True`` when a local cloud
        mirror/backup has files, ``False`` when the store has no cloud support,
        else ``None`` (unknown without a full download).
        """
        supported = store in self._strategies
        status = self._base_status(store, game_id, supported)
        if not supported:
            return status
        save_dir: str | None = ""
        try:
            save_dir = self.get_local_save_dir(store, game_id)
        except Exception as e:
            logger.debug("[CloudSave] status: get_local_save_dir failed: %s", e)
        status["browse_start"] = self._browse_start(game_id, save_dir)
        status.update(self._local_save_status(save_dir))
        status["save_path_is_override"] = self._has_save_path_override(game_id)
        remote = self._cloud_snapshot(store, game_id)
        cloud_info = await self._real_cloud_info(store, game_id)
        self._merge_cloud_info(status, cloud_info, remote)
        return status

    async def _real_cloud_info(
        self, store: str, game_id: str
    ) -> dict[str, Any] | None:
        """Real store-cloud save info via the strategy, timeout-bounded."""
        strat = self._strategies.get(store)
        if strat is None:
            return None
        try:
            info: dict[str, Any] | None = await asyncio.wait_for(
                strat.get_cloud_save_info(game_id), timeout=20,
            )
            return info
        except Exception:
            return None

    def _cloud_snapshot(self, store: str, game_id: str) -> dict[str, Any]:
        """Best-effort ``{timestamp, file_count, total_bytes}`` for the
        cloud-side copy, to populate the conflict modal.

        Uses the plugin's local cloud backup (``_cloud_root``) when present,
        else the most recent versioned save backup — both are local and
        cheap. A live store-cloud listing would mean a full download just to
        render a modal, so we approximate from the nearest local mirror.
        """
        from .safety import latest_backup_snapshot, snapshot
        if self._cloud_root:
            remote_dir = Path(self._cloud_root) / store / game_id
            if remote_dir.is_dir():
                return snapshot(remote_dir)
        return latest_backup_snapshot(store, game_id)
