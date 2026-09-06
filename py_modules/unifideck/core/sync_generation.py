"""Sync generation identity — run ids and post-sync chain gating.

Two facts that ``SyncService`` needs but that have no business living in
the already-cap-bound host file.

**Run ids.** ``POST_SYNC_PHASE_CHANGED`` used to carry no identity, so a
phase-done event could not be attributed to the run that produced it.
Both drain sites — ``SyncService._on_post_sync_phase`` and the
frontend's ``sync-store`` — simply removed the phase name from a single
mutable pending set. A superseded run finishing late therefore drained
the *current* run's set, which flipped the progress bar to complete and
popped the Steam-restart modal while the live generation was still
downloading artwork.

Measured on 2026-08-29 (``2026-08-29 01.47.02.log``): six back-to-back
store logins produced seven syncs, and three artwork batches were alive
simultaneously. The 645-game generation reported its artwork phase done
at 02:17:12 against a library that had been 1242 games since 02:15:00;
the 1229-game generation reported at 02:22:30. Neither belonged to the
run whose pending set they cleared.

**Chain gating.** The post-sync chain (metadata → artwork → compat) runs
for minutes outside ``SyncService._lock``. Re-running it for a store set
and game count identical to the last *completed* chain is pure waste: in
the same session the seventh sync re-entered the whole chain over 1242
games and reconciled ``added=0 removed=0``. :meth:`SyncGeneration.
chain_is_redundant` is the check that skips it.

The counterpart on the emit side is that every post-sync service echoes
``run_id`` back on its phase-done event; see the ``run_id`` plumbing in
``sync_finalize_mixin._emit_complete`` and the three module handlers.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Sentinel used when a phase-done event carries no ``run_id`` at all.
#: Treated as "belongs to the current run" so an un-migrated emitter (or
#: a replayed event from before this field existed) still drains its
#: phase rather than stranding the set forever. Failing open here is the
#: safe direction: a stranded set stalls the progress bar and the restart
#: modal permanently, whereas an over-eager drain is what the run-id
#: check already exists to catch on the paths that do send it.
UNTAGGED_RUN_ID = -1


def run_id_of(payload: dict[str, Any] | None) -> int:
    """Read a generation id out of an event payload, coercing safely.

    Every post-sync module needs this to echo the id it was handed, and a
    plain ``payload.get("run_id", UNTAGGED_RUN_ID)`` is not enough: the
    key can be present and ``None`` (a partially-migrated emitter, or a
    replayed event), which would then be forwarded as ``None`` and fail
    the ``int`` contract on the next hop.

    Args:
        payload: any event kwargs mapping, or ``None``.

    Returns:
        The integer generation id, or :data:`UNTAGGED_RUN_ID` when the
        payload carries no usable one. ``bool`` is rejected explicitly —
        it is an ``int`` subclass and would otherwise read as run 0 or 1.
    """
    raw = (payload or {}).get("run_id")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return UNTAGGED_RUN_ID
    return raw


class SyncGeneration:
    """Monotonic run counter plus last-completed-chain bookkeeping.

    One instance per :class:`~unifideck.core.sync_service.SyncService`.
    Not thread-safe and does not need to be — every mutation happens on
    the plugin's single asyncio loop.
    """

    def __init__(self) -> None:
        """Start at generation 0 with no completed chain recorded."""
        self._run_id = 0
        self._last_chain_covered: tuple[frozenset[str], int] | None = None

    @property
    def run_id(self) -> int:
        """The current generation id."""
        return self._run_id

    def begin(self) -> int:
        """Bump to the next generation and return it.

        Called once per ``_run_sync``, before ``SYNC_STARTED`` is
        emitted, so the id on the started event matches the ids on that
        run's phase-done events.
        """
        self._run_id += 1
        return self._run_id

    def is_stale(self, payload: dict[str, Any] | None) -> bool:
        """Whether a phase-done payload belongs to a superseded run.

        Args:
            payload: the ``POST_SYNC_PHASE_CHANGED`` kwargs. A missing
                or non-integer ``run_id`` reads as
                :data:`UNTAGGED_RUN_ID` and is never stale — see that
                constant for why this fails open.

        Returns:
            True when the event came from an older generation and must
            not touch the current run's pending-phase set.
        """
        run_id = run_id_of(payload)
        if run_id == UNTAGGED_RUN_ID:
            return False
        return run_id != self._run_id

    def chain_is_redundant(self, stores: frozenset[str], games: int) -> bool:
        """Whether the post-sync chain would repeat the last completed one.

        Args:
            stores: the stores this run fetched.
            games: total game count this run produced.

        Returns:
            True when a chain has already completed for exactly this
            store set and game count, so metadata/artwork/compat have
            nothing new to look at.
        """
        return self._last_chain_covered == (stores, games)

    def record_chain_complete(
        self, stores: frozenset[str], games: int,
    ) -> None:
        """Remember the store set + game count a finished chain covered.

        Only call this once every registered phase has reported done for
        the current generation. Recording a chain that was cancelled
        mid-flight would let the next identical run skip work that never
        actually happened — which is precisely the state that left 13
        Ubisoft games with no artwork at all.
        """
        self._last_chain_covered = (stores, games)

    def forget_chain(self) -> None:
        """Drop the completed-chain record so the next run cannot skip.

        Used when the library is reset or a force sync asks for a full
        refresh: the caches the chain partitions on are gone, so the
        "nothing changed" conclusion no longer holds.
        """
        self._last_chain_covered = None
