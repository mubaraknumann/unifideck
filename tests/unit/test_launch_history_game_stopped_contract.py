"""The circuit breaker must clear when a launch actually succeeds.

Audit correction C-2 (`docs/architecture-audit.md`), register item 46.

``_on_game_stopped`` read ``kwargs.get("rc")`` and ``kwargs.get("elapsed")``.
No ``GAME_STOPPED`` emitter has ever sent either name — ``notify_game_stopped``
sends ``exit_code`` and ``CANONICAL_SCHEMA`` declares
``exit_code``/``elapsed_seconds`` — so ``rc`` was always ``None``, the handler
returned at its ``rc is None`` guard on every stop, and :meth:`record_success`
had **no reachable call site anywhere in the tree**. A game could accumulate
launch failures and never clear them except by waiting out the window.

These tests assert against the **emitter's real kwargs**, not a hand-written
payload. §1.1.1 of the audit shipped a broken fix twice because its two guards
each pinned one side of a mismatch and agreed with nothing; the payloads below
are copied from ``rpc/mixins/launch.py::notify_game_stopped``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.services.launch_history import (
    FAILURE_KIND_FAST_BOOT,
    LaunchHistoryService,
)

GAME_KEY = "epic:Salt"
STORE, GAME_ID = "epic", "Salt"


def _service(tmp_path: Path) -> LaunchHistoryService:
    """A service with no bus — handlers are driven directly."""
    return LaunchHistoryService(config=None, storage_path=tmp_path / "h.json")


def _failure_count(svc: LaunchHistoryService) -> int:
    if not svc._path.exists():
        return 0
    data: dict[str, Any] = json.loads(svc._path.read_text())
    return len(data.get(GAME_KEY, {}).get("failures", []))


# --- the payload the live emitter actually sends -------------------------

def _stopped_kwargs(exit_code: int) -> dict[str, Any]:
    """Exactly what ``notify_game_stopped`` puts on the bus."""
    return {
        "store": STORE,
        "game_id": GAME_ID,
        "app_id": 1234567890,
        "exit_code": exit_code,
    }


@pytest.mark.asyncio
async def test_clean_exit_clears_recorded_failures(tmp_path: Path) -> None:
    """The regression. Fails against the ``rc``/``elapsed`` version."""
    svc = _service(tmp_path)
    svc.record_failure(GAME_KEY, FAILURE_KIND_FAST_BOOT, "rc=1")
    svc.record_failure(GAME_KEY, FAILURE_KIND_FAST_BOOT, "rc=1")
    assert _failure_count(svc) == 2

    await svc._on_game_stopped(**_stopped_kwargs(0))

    assert _failure_count(svc) == 0, (
        "a clean exit must wipe the failure history — record_success was "
        "unreachable while the handler read 'rc'"
    )


@pytest.mark.asyncio
async def test_circuit_reopens_for_play_after_a_good_launch(tmp_path: Path) -> None:
    """The user-visible half: a tripped game becomes playable again."""
    svc = _service(tmp_path)
    for _ in range(svc.threshold()):
        svc.record_failure(GAME_KEY, FAILURE_KIND_FAST_BOOT, "rc=1")
    assert svc.is_circuit_open(GAME_KEY)[0] is True

    await svc._on_game_stopped(**_stopped_kwargs(0))

    assert svc.is_circuit_open(GAME_KEY)[0] is False


@pytest.mark.asyncio
async def test_a_fast_non_zero_exit_is_recorded_as_a_failure(tmp_path: Path) -> None:
    """No emitter sends an elapsed time, so it is measured here.

    ``_on_game_launched`` stamps a monotonic start; without it ``elapsed``
    defaults to 0.0, which still classifies as fast-boot — this test pins the
    stamped path so the two handlers stay a pair.
    """
    svc = _service(tmp_path)
    await svc._on_game_launched(store=STORE, game_id=GAME_ID, app_id=1, title="")
    assert GAME_KEY in svc._started_at

    await svc._on_game_stopped(**_stopped_kwargs(1))

    assert _failure_count(svc) == 1
    assert GAME_KEY not in svc._started_at, "the stamp must not leak"


@pytest.mark.asyncio
async def test_a_long_session_ending_non_zero_is_not_a_launch_failure(
    tmp_path: Path,
) -> None:
    """A game that ran for an hour then crashed did not fail to *launch*."""
    svc = _service(tmp_path)
    kwargs = _stopped_kwargs(1)
    kwargs["elapsed_seconds"] = svc.fast_boot_seconds() + 60.0

    await svc._on_game_stopped(**kwargs)

    assert _failure_count(svc) == 0


@pytest.mark.asyncio
async def test_signal_termination_is_ignored(tmp_path: Path) -> None:
    """User pressed Stop — rc > 128 is a signal, not a launch failure."""
    svc = _service(tmp_path)
    await svc._on_game_stopped(**_stopped_kwargs(143))
    assert _failure_count(svc) == 0


@pytest.mark.asyncio
async def test_the_launcher_subprocess_payload_still_no_ops(tmp_path: Path) -> None:
    """The other bus is unchanged, deliberately.

    ``services/launcher/orchestrator.py`` emits ``store``/``game_id`` alone on
    the launcher subprocess bus. That carries no exit code, so it cannot
    classify anything and must stay a no-op rather than guess ``0`` and wipe a
    real failure history.
    """
    svc = _service(tmp_path)
    svc.record_failure(GAME_KEY, FAILURE_KIND_FAST_BOOT, "rc=1")

    await svc._on_game_stopped(store=STORE, game_id=GAME_ID)

    assert _failure_count(svc) == 1


@pytest.mark.asyncio
async def test_the_handler_reads_the_names_the_schema_declares(tmp_path: Path) -> None:
    """Pins the contract itself, so a rename on either side is caught.

    This is the check that was missing: ``validate_event_schemas.py`` is
    emit-side only and never compared a subscriber's ``kwargs.get`` keys
    against the declared payload.
    """
    svc = _service(tmp_path)
    svc.record_failure(GAME_KEY, FAILURE_KIND_FAST_BOOT, "rc=1")

    # The pre-fix names must NOT be honoured any more.
    await svc._on_game_stopped(store=STORE, game_id=GAME_ID, rc=0, elapsed=0.0)

    assert _failure_count(svc) == 1, (
        "'rc' is not part of the GAME_STOPPED contract; honouring it would "
        "mean the handler accepts a payload no emitter sends"
    )
