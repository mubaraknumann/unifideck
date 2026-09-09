"""A superseded sync must not be able to complete the current one.

``POST_SYNC_PHASE_CHANGED`` used to carry no run identity. Both drain
sites — ``SyncService._on_post_sync_phase`` and the frontend's
``sync-store`` — just removed the phase name from a single mutable set,
so a phase-done arriving from a run that had already been replaced
cleared the *live* run's set. When the set emptied the sync was marked
complete and the Steam-restart modal fired, while the current
generation was still downloading artwork.

Measured 2026-08-29 (``2026-08-29 01.47.02.log``): six back-to-back
store logins produced seven syncs and left three artwork batches alive
at once. The 645-game generation announced its artwork phase done at
02:17:12 against a library that had been 1242 games since 02:15:00; the
1229-game generation announced at 02:22:30. Neither belonged to the run
whose pending set it cleared.

``core/sync_generation.py`` adds the run id; these tests pin both the
"ignore the stale one" behaviour and the fail-open path for an event
that carries no id at all (an un-migrated emitter, or a replayed event
from before the field existed). Failing open matters: a stranded
pending set hangs the progress bar and the restart modal permanently,
which is a worse failure than the one being fixed.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.core.sync_generation import (
    UNTAGGED_RUN_ID,
    SyncGeneration,
    run_id_of,
)
from unifideck.core.sync_progress import SyncProgress
from unifideck.core import sync_service as ss


class _Bus:
    """Minimal bus stand-in — only what ``_on_post_sync_phase`` touches."""

    def __init__(self) -> None:
        self.progress_cleared = 0

    def set_sync_progress(self, value: Any) -> None:
        if value is None:
            self.progress_cleared += 1


class _Svc(ss.SyncService):
    """SyncService with construction bypassed — this exercises one method."""

    def __init__(self) -> None:  # noqa: D107 - deliberate no-super
        self._bus = _Bus()
        self._progress = SyncProgress()
        self._generation = SyncGeneration()
        self._post_sync_pending: set[str] = set()
        self._all_games = {}
        self.backfills = 0

    def _spawn_size_backfill(self) -> None:
        self.backfills += 1


# ── run_id_of ───────────────────────────────────────────


def test_run_id_of_reads_an_integer():
    assert run_id_of({"run_id": 7}) == 7


@pytest.mark.parametrize(
    "payload",
    [{}, None, {"run_id": None}, {"run_id": "3"}, {"run_id": 1.5}],
)
def test_run_id_of_falls_back_when_unusable(payload):
    assert run_id_of(payload) == UNTAGGED_RUN_ID


def test_run_id_of_rejects_bool():
    """``bool`` is an ``int`` subclass; True must not read as run 1."""
    assert run_id_of({"run_id": True}) == UNTAGGED_RUN_ID


# ── SyncGeneration ──────────────────────────────────────


def test_begin_increments_monotonically():
    gen = SyncGeneration()
    assert gen.run_id == 0
    assert [gen.begin(), gen.begin(), gen.begin()] == [1, 2, 3]
    assert gen.run_id == 3


def test_is_stale_only_for_a_different_tagged_run():
    gen = SyncGeneration()
    gen.begin()  # run 1
    gen.begin()  # run 2
    assert gen.is_stale({"run_id": 1}) is True
    assert gen.is_stale({"run_id": 2}) is False
    # Fail open — an untagged event is never treated as stale.
    assert gen.is_stale({}) is False
    assert gen.is_stale({"run_id": None}) is False


def test_chain_redundancy_tracks_stores_and_count():
    gen = SyncGeneration()
    stores = frozenset({"epic", "gog"})
    assert gen.chain_is_redundant(stores, 645) is False
    gen.record_chain_complete(stores, 645)
    assert gen.chain_is_redundant(stores, 645) is True
    # A new store, or a changed game count, is not redundant.
    assert gen.chain_is_redundant(stores | {"ubisoft"}, 645) is False
    assert gen.chain_is_redundant(stores, 646) is False
    gen.forget_chain()
    assert gen.chain_is_redundant(stores, 645) is False


# ── the drain site ──────────────────────────────────────


def test_stale_phase_done_does_not_drain_the_live_run():
    """The regression itself: run 1 finishing late must not complete run 2."""
    svc = _Svc()
    svc._generation.begin()          # run 1 starts
    svc._generation.begin()          # run 2 supersedes it
    svc._post_sync_pending = {"metadata", "artwork", "proton_meta"}

    # Run 1's orphaned artwork batch finally reports done.
    svc._on_post_sync_phase(
        phase="artwork", active=False, total=645, run_id=1,
    )

    assert svc._post_sync_pending == {"metadata", "artwork", "proton_meta"}
    assert svc._progress.status != "complete"
    assert svc.backfills == 0


def test_current_run_phase_done_drains_normally():
    svc = _Svc()
    svc._generation.begin()
    svc._generation.begin()          # run 2 is current
    svc._post_sync_pending = {"metadata", "artwork"}

    svc._on_post_sync_phase(phase="metadata", active=False, run_id=2)
    assert svc._post_sync_pending == {"artwork"}

    svc._on_post_sync_phase(phase="artwork", active=False, run_id=2)
    assert svc._post_sync_pending == set()
    assert svc._progress.status == "complete"
    assert svc.backfills == 1


def test_untagged_phase_done_still_drains():
    """Fail open: a phase-done with no run id must not strand the set."""
    svc = _Svc()
    svc._generation.begin()
    svc._post_sync_pending = {"artwork"}

    svc._on_post_sync_phase(phase="artwork", active=False)

    assert svc._post_sync_pending == set()
    assert svc._progress.status == "complete"


def test_active_phase_events_are_ignored():
    """Only the completion flank matters."""
    svc = _Svc()
    svc._generation.begin()
    svc._post_sync_pending = {"artwork"}

    svc._on_post_sync_phase(phase="artwork", active=True, run_id=1)

    assert svc._post_sync_pending == {"artwork"}


def test_cancelled_run_does_not_record_a_completed_chain():
    """A cancelled chain must never let the next identical run skip.

    Recording it would mean the next sync short-circuits metadata,
    artwork and compat on the strength of work that never happened —
    exactly the state that left thirteen Ubisoft games with no artwork.
    """
    svc = _Svc()
    svc._generation.begin()
    svc._all_games = {"ubisoft": []}
    svc._post_sync_pending = {"artwork"}
    svc._progress.status = "cancelled"

    svc._on_post_sync_phase(phase="artwork", active=False, run_id=1)

    assert svc._post_sync_pending == set()
    assert svc._generation.chain_is_redundant(frozenset({"ubisoft"}), 0) is False
    assert svc.backfills == 0


# ── the chain-skip gate ─────────────────────────────────
#
# Six store logins produced seven syncs, each re-running the whole
# metadata → artwork → compat chain over the growing cumulative library.
# The seventh covered exactly the store set and game count of the sixth
# and reconciled ``added=0 removed=0 reclaimed=997`` — its chain was pure
# waste. ``skip_chain`` on SYNC_COMPLETE is what stops that repeat.


class _FinalizeSvc(_Svc):
    """Adds the finalize-mixin state the redundancy gate reads."""

    def __init__(self) -> None:
        super().__init__()
        self._registered_phases = {"metadata", "artwork"}


def _libraries(counts: dict[str, int]) -> dict[str, list[Any]]:
    return {store: [object()] * n for store, n in counts.items()}


def test_chain_is_not_skipped_before_any_chain_completes():
    svc = _FinalizeSvc()
    libs = _libraries({"amazon": 105})
    assert svc._chain_is_redundant(
        libs, 105, is_force=False, resync_artwork=False,
    ) is False


def test_chain_is_skipped_when_nothing_changed():
    """Sync #7 of the measured session: same stores, same count."""
    svc = _FinalizeSvc()
    stores = {"amazon", "battlenet", "epic", "gog", "microsoft", "ubisoft"}
    svc._all_games = {s: [object()] for s in stores}
    svc._generation.record_chain_complete(frozenset(stores), 1242)

    libs = _libraries(dict.fromkeys(stores, 1))
    assert svc._chain_is_redundant(
        libs, 1242, is_force=False, resync_artwork=False,
    ) is True


def test_a_new_store_reruns_the_chain():
    """The user's rule: an increase in store count must redo the modules."""
    svc = _FinalizeSvc()
    five = {"amazon", "battlenet", "epic", "gog", "microsoft"}
    svc._generation.record_chain_complete(frozenset(five), 1229)

    six = five | {"ubisoft"}
    assert svc._chain_is_redundant(
        _libraries(dict.fromkeys(six, 1)), 1242,
        is_force=False, resync_artwork=False,
    ) is False


def test_force_and_resync_never_skip():
    """Both exist to redo work the caches would short-circuit."""
    svc = _FinalizeSvc()
    stores = frozenset({"epic"})
    svc._generation.record_chain_complete(stores, 295)
    libs = _libraries({"epic": 295})

    assert svc._chain_is_redundant(
        libs, 295, is_force=True, resync_artwork=False,
    ) is False
    assert svc._chain_is_redundant(
        libs, 295, is_force=False, resync_artwork=True,
    ) is False
