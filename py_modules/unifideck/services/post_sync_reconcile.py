"""Boot-time reconcile for the post-sync data components.

The post-sync chain (metadata → artwork → compat) is a set of background
tasks in the plugin process, and that process is restarted independently
of both Steam and ``plugin_loader`` — most often *right after a sync*,
because that is exactly when the user is told to restart Steam so new
shortcuts and artwork load. A chain interrupted that way was simply lost:
nothing at boot ever checked whether the library's metadata, artwork or
compat data were actually complete, so the gap survived until the user
happened to run another sync.

Measured on 2026-08-29. Six store logins, seven syncs, plugin unloaded at
02:23:21 while two artwork batches were still in flight. On the next boot
(02:23:46) the only checks that ran were ``orphan-scan`` — which looks at
orphaned *shortcuts*, not artwork — and the size-backfill resume. The
residue on disk afterwards:

    store        games  incomplete  zero-art
    amazon         105           1         0
    battlenet       17           0         0
    epic           295          17         0
    gog            228          19         0
    microsoft      584          61         0
    ubisoft         13          13        13     ← last store logged in
    total         1242         111        13

All thirteen Ubisoft titles had no artwork at all, because Ubisoft's batch
never got to run before the restart. This service is what notices that on
the next boot and fills it in.

Scope — deliberately narrow
===========================
Covers the three post-sync *data* components and nothing else:

* **artwork** — per-kind gaps via :func:`artwork.fetcher.get_missing_kinds`
* **metadata** — :meth:`MetadataService._has_complete_metadata`
* **compat**   — :meth:`CompatibilityService._partition_games`

It deliberately **does not write shortcuts.vdf**. Shortcut reconciliation
is the most destructive operation in the tree and its safety rests on
``_sweepable_stores``: a store may only have shortcuts swept when it
answered a sync *this run*. At boot no store has answered anything, so
there is no honest sweepable set, and a boot-time writer would be
re-opening the exact hole that once deleted a signed-out store's entire
library (audit §3.5, finding B). Missing shortcuts are covered by the
existing boot orphan-scan plus the next sync's reconcile.

Reuses the existing per-game entry points rather than reimplementing
them: ``ArtworkService.fetch_artwork``, ``MetadataService.enrich``, and
``CompatibilityService.repair_missing``. Each already owns its own
concurrency gate and cache-write semantics.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events

if TYPE_CHECKING:
    from unifideck.core.types import Game
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

# Delay before the boot pass runs. Boot already does a fair amount of
# work (store registration, binary signature checks, prefix-bridge sweep,
# size-backfill resume) and the update sweep lands at 45s; sitting after
# all of it keeps the reconcile off the critical path to a usable UI.
BOOT_DELAY_SECONDS = 90

# Upper bound on games repaired in one pass, per component. A pass is
# meant to close the tail left by an interrupted chain, not to stand in
# for a full sync. Whatever is left is reported and picked up by the next
# pass or the next sync — silent truncation would read as "all clear".
MAX_REPAIR_PER_COMPONENT = 400

# Concurrency for the metadata repair loop. ArtworkService and
# CompatibilityService each apply their own semaphore internally;
# ``MetadataService.enrich`` is a bare per-game call, so it needs one here.
METADATA_REPAIR_CONCURRENCY = 8

# Host used for the reachability probe. The 02:23:48 boot in the log above
# failed DNS outright ("Temporary failure in name resolution"), and a pass
# that burns its one shot while offline would report zero gaps repaired
# and look like success.
_PROBE_HOST = "api.steamgriddb.com"
_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass
class ReconcileReport:
    """What one reconcile pass found and what it managed to fix."""

    ran: bool = False
    skipped_reason: str = ""
    total_games: int = 0
    artwork_gaps: int = 0
    artwork_repaired: int = 0
    metadata_gaps: int = 0
    metadata_repaired: int = 0
    compat_gaps: int = 0
    compat_repaired: int = 0
    #: Games whose artwork is still incomplete after the pass, capped for
    #: logging. Non-empty here is the signal that a follow-up is needed.
    artwork_remaining: list[str] = field(default_factory=list)

    @property
    def repaired_total(self) -> int:
        """Total games touched across every component."""
        return (
            self.artwork_repaired
            + self.metadata_repaired
            + self.compat_repaired
        )

    @property
    def gap_total(self) -> int:
        """Total gaps detected across every component."""
        return self.artwork_gaps + self.metadata_gaps + self.compat_gaps


class PostSyncReconcileService:
    """Detects and repairs post-sync data gaps left by an interrupted run."""

    def __init__(
        self,
        bus: EventBus,
        sync_service: Any,
        artwork: Any = None,
        metadata: Any = None,
        compat: Any = None,
    ) -> None:
        """Store collaborators. Every service is optional so a partial
        bootstrap (or a test) can construct this without the full container;
        a missing collaborator simply drops that component from the pass."""
        self._bus = bus
        self._sync = sync_service
        self._artwork = artwork
        self._metadata = metadata
        self._compat = compat
        self._task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────

    def start(self) -> None:
        """Schedule the delayed boot pass. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._boot_pass(), name="post-sync-reconcile",
        )
        logger.info(
            "[PostSyncReconcile] boot pass scheduled in %ds",
            BOOT_DELAY_SECONDS,
        )

    async def stop(self) -> None:
        """Cancel a pending or in-flight pass."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def _boot_pass(self) -> None:
        """Sleep out the boot delay, then run one pass."""
        try:
            await asyncio.sleep(BOOT_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await self.run(reason="boot")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a repair pass take the plugin down with it — this
            # is opportunistic cleanup, not a load-bearing path.
            logger.exception("[PostSyncReconcile] pass failed")

    # ── the pass ─────────────────────────────────────────

    async def run(self, *, reason: str = "manual") -> ReconcileReport:
        """Detect gaps across every component and repair what it can."""
        report = ReconcileReport()
        blocked = await self._blocked_reason()
        if blocked:
            report.skipped_reason = blocked
            logger.info("[PostSyncReconcile] skipped — %s", blocked)
            return report

        games = self._library()
        report.total_games = len(games)
        if not games:
            report.skipped_reason = "empty library"
            logger.info("[PostSyncReconcile] skipped — no cached library")
            return report

        report.ran = True
        logger.info(
            "[PostSyncReconcile] %s pass over %d game(s)", reason, len(games),
        )
        await self._reconcile_artwork(games, report)
        await self._reconcile_metadata(games, report)
        await self._reconcile_compat(games, report)

        logger.info(
            "[PostSyncReconcile] done — artwork %d/%d, metadata %d/%d, "
            "compat %d/%d (gaps found/repaired)",
            report.artwork_gaps, report.artwork_repaired,
            report.metadata_gaps, report.metadata_repaired,
            report.compat_gaps, report.compat_repaired,
        )
        if report.artwork_remaining:
            logger.warning(
                "[PostSyncReconcile] %d game(s) still missing artwork after "
                "the pass, e.g. %s",
                len(report.artwork_remaining),
                ", ".join(report.artwork_remaining[:5]),
            )
        await self._announce(report)
        return report

    async def _blocked_reason(self) -> str:
        """Why this pass must not run now, or ``""`` when it may."""
        # A sync in flight owns the same caches and grid dir; the chain it
        # is running will resolve these gaps itself.
        try:
            if self._bus.get_sync_progress() is not None:
                return "a sync is in flight"
        except Exception as e:
            # A bus without the accessor (test double, partial bootstrap)
            # must not block the pass — worst case we run alongside a sync
            # and each game's gap check simply finds nothing to do.
            logger.debug(
                "[PostSyncReconcile] sync-progress probe unavailable: %s", e,
            )
        if not await self._network_reachable():
            return "network unreachable"
        return ""

    @staticmethod
    async def _network_reachable() -> bool:
        """Cheap DNS probe — every repair path needs the network."""
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(_PROBE_HOST, 443, type=socket.SOCK_STREAM),
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError):
            return False
        return True

    def _library(self) -> list[Game]:
        """The cached library, or an empty list when unavailable."""
        try:
            games = self._sync.get_all_games()
        except Exception:
            logger.exception("[PostSyncReconcile] could not read the library")
            return []
        return [g for g in games if getattr(g, "app_id", None)]

    # ── artwork ──────────────────────────────────────────

    async def _reconcile_artwork(
        self, games: list[Game], report: ReconcileReport,
    ) -> None:
        """Fill per-kind artwork gaps for games that have any."""
        if self._artwork is None:
            return
        grid_dir = getattr(self._artwork, "grid_dir", "") or ""
        if not grid_dir:
            logger.warning(
                "[PostSyncReconcile] artwork skipped — grid dir unset",
            )
            return
        from unifideck.services.artwork.fetcher import get_missing_kinds

        gaps: list[tuple[Game, set[str]]] = []
        for game in games:
            missing = await get_missing_kinds(grid_dir, game.app_id)
            if missing:
                gaps.append((game, missing))
        report.artwork_gaps = len(gaps)
        if not gaps:
            return

        batch = gaps[:MAX_REPAIR_PER_COMPONENT]
        if len(gaps) > len(batch):
            logger.info(
                "[PostSyncReconcile] artwork: %d gap(s), repairing %d this "
                "pass (the rest carry to the next one)",
                len(gaps), len(batch),
            )
        results = await asyncio.gather(
            *(self._repair_one_artwork(g, kinds) for g, kinds in batch),
            return_exceptions=True,
        )
        report.artwork_repaired = sum(1 for r in results if r is True)
        # Re-check so the report describes disk truth, not attempts.
        for game, _kinds in batch:
            if await get_missing_kinds(grid_dir, game.app_id):
                report.artwork_remaining.append(game.title)

    async def _repair_one_artwork(self, game: Game, kinds: set[str]) -> bool:
        """Fetch exactly the missing kinds for one game."""
        try:
            result = await self._artwork.fetch_artwork(
                game.app_id, game.store, game.store_game_id, game.title,
                extras=getattr(game, "metadata", None),
                only_kinds=kinds,
            )
        except Exception as e:
            logger.debug(
                "[PostSyncReconcile] artwork repair failed for %s: %s",
                game.title, e,
            )
            return False
        return any(result.get(k) for k in kinds)

    # ── metadata ─────────────────────────────────────────

    async def _reconcile_metadata(
        self, games: list[Game], report: ReconcileReport,
    ) -> None:
        """Enrich games whose metadata cache entry is missing or partial."""
        if self._metadata is None:
            return
        try:
            pending = [
                g for g in games
                if not self._metadata._has_complete_metadata(g)
            ]
        except Exception:
            logger.exception("[PostSyncReconcile] metadata partition failed")
            return
        report.metadata_gaps = len(pending)
        if not pending:
            return
        batch = pending[:MAX_REPAIR_PER_COMPONENT]
        if len(pending) > len(batch):
            logger.info(
                "[PostSyncReconcile] metadata: %d gap(s), repairing %d this "
                "pass (the rest carry to the next one)",
                len(pending), len(batch),
            )
        gate = asyncio.Semaphore(METADATA_REPAIR_CONCURRENCY)

        async def _one(game: Game) -> bool:
            async with gate:
                try:
                    await self._metadata.enrich(game)
                except Exception as e:
                    logger.debug(
                        "[PostSyncReconcile] metadata repair failed for "
                        "%s: %s", game.title, e,
                    )
                    return False
                return True

        results = await asyncio.gather(
            *(_one(g) for g in batch), return_exceptions=True,
        )
        report.metadata_repaired = sum(1 for r in results if r is True)

    # ── compat ───────────────────────────────────────────

    async def _reconcile_compat(
        self, games: list[Game], report: ReconcileReport,
    ) -> None:
        """Resolve ProtonDB / Deck-Verified ratings still missing."""
        if self._compat is None:
            return
        try:
            _cached, pending = self._compat._partition_games(games)
        except Exception:
            logger.exception("[PostSyncReconcile] compat partition failed")
            return
        report.compat_gaps = len(pending)
        if not pending:
            return
        batch = pending[:MAX_REPAIR_PER_COMPONENT]
        if len(pending) > len(batch):
            logger.info(
                "[PostSyncReconcile] compat: %d gap(s), repairing %d this "
                "pass (the rest carry to the next one)",
                len(pending), len(batch),
            )
        try:
            report.compat_repaired = await self._compat.repair_missing(batch)
        except Exception:
            logger.exception("[PostSyncReconcile] compat repair failed")

    # ── user-facing summary ──────────────────────────────

    async def _announce(self, report: ReconcileReport) -> None:
        """Toast a summary when the pass actually fixed something.

        Uses ``LAUNCHER_STAGE`` because it is the plugin's only wired
        user-facing toast channel — see the note on the enum member. A
        pass that found nothing stays silent; there is nothing to tell.
        """
        if report.repaired_total <= 0:
            return
        try:
            await self._bus.emit(
                Events.LAUNCHER_STAGE,
                i18n_title_key="reconcile.repairedTitle",
                i18n_key="reconcile.repairedBody",
                severity="info",
                i18n_params={
                    "artwork": report.artwork_repaired,
                    "metadata": report.metadata_repaired,
                    "compat": report.compat_repaired,
                },
            )
        except Exception:
            logger.exception("[PostSyncReconcile] summary toast failed")
