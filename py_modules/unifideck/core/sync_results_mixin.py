"""Result-aggregation mixin for :class:`SyncService`.

Extracted from ``core/sync_service.py`` to keep the host file
under the 550 LOC volumetry cap. Single responsibility now:
build the final ``SyncResult`` from per-store libraries +
errors + timings, and log the summary line.

Earlier revisions of this module also housed
``_apply_dedup_and_emit`` and ``_tracked_stores`` for
cross-store dedup. Dedup was removed (duplicate titles across
stores now show as distinct shortcuts thanks to the
store-scoped ``generate_app_id`` identity); the dead code was
deleted with the appid-generation refactor.
"""

from __future__ import annotations

import logging

from unifideck.utils.config_helpers import get_cfg

from .types import Game, SyncResult

logger = logging.getLogger(__name__)
_DEFAULT_TRACKED_STORES = ("epic", "gog", "amazon", "ubisoft", "battlenet")


class _SyncResultsMixin:
    """Result helpers for :class:`SyncService`."""

    def _aggregate_results(
        self,
        libraries: dict[str, list[Game]],
        errors: dict[str, str],
        duration_ms: int,
        total_stores: int,
    ) -> SyncResult:
        """Build the final ``SyncResult`` + log the summary line.

        Partial-success heuristic: ``success=True`` if at least
        one store contributed.
        """
        merged = self._flatten(libraries)  # type: ignore[attr-defined]  # provided by _SyncQueriesMixin
        merged = self._maybe_collapse_duplicates(merged)
        merged = self._maybe_annotate_duplicate_groups(merged)
        logger.info(
            "[SyncService] sync complete — %d games across %d stores "
            "in %dms (%d errors)",
            len(merged), len(libraries), duration_ms, len(errors),
        )
        return SyncResult(
            success=len(errors) < total_stores,
            games=merged,
            count=len(merged),
            duration_ms=duration_ms,
            error=None if not errors else f"{len(errors)}_stores_failed",
        )

    def _maybe_collapse_duplicates(self, games: list[Game]) -> list[Game]:
        """Optionally collapse cross-store duplicates (disabled by default).

        No-op unless ``dedup.cross_store_enabled`` is true — the default
        keeps each store's copy of a title as its own shortcut. When
        enabled, a title owned on multiple ``dedup.tracked_stores`` is
        collapsed to one (see :mod:`unifideck.core.cross_source_dedupe`).
        Groundwork for the "one shortcut per game" feature.
        """
        config = getattr(self, "_config", None)
        if not get_cfg(config, "dedup.cross_store_enabled", False):
            return games
        from unifideck.core.cross_source_dedupe import collapse_duplicates

        tracked = get_cfg(
            config, "dedup.tracked_stores", list(_DEFAULT_TRACKED_STORES),
        )
        collapsed = collapse_duplicates(games, tracked_stores=tracked)
        if len(collapsed) != len(games):
            logger.info(
                "[SyncService] cross-store dedup collapsed %d → %d games",
                len(games),
                len(collapsed),
            )
        return collapsed

    def _maybe_annotate_duplicate_groups(self, games: list[Game]) -> list[Game]:
        """Stamp display-only ``dedupe_group_id``/``edition_label`` fields.

        Unlike :meth:`_maybe_collapse_duplicates`, this never changes the
        game count — it only powers the "All Games" grid's multi-store
        card grouping and the detail-page store switcher. Gated by
        ``dedup.ui_grouping_enabled`` (default true) purely as an escape
        hatch, not because it's expected to need disabling.

        Thin wrapper around
        :func:`~unifideck.core.game_grouping.annotate_duplicate_groups_if_enabled`
        — the same gate + Steam-owned-lookup logic also runs on cache
        load and on a frontend Steam-owned-titles push, so it lives
        there rather than only here.
        """
        from unifideck.core.game_grouping import (
            annotate_duplicate_groups_if_enabled,
        )

        config = getattr(self, "_config", None)
        return annotate_duplicate_groups_if_enabled(games, config)
