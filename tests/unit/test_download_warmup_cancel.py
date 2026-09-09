"""Cancelling the post-install prefix warmup, and the stale-row race.

Two bugs, one code path, reported together as a GOG install stuck on
"SETTING UP GAME... / Initializing prefix and syncing cloud saves..." whose
Cancel answered "Cancel Failed: not_found".

1. **The row outlived the operation.** ``_on_install_success`` emits
   DOWNLOAD_COMPLETE *before* ``_run_install``'s ``finally`` pops the item from
   ``_running``. The frontend refetches the queue the moment it sees that
   event, so it cached a row the backend was already finished with — measured
   243 ms of exposure on a GOG install and 6.5 s on an Epic one. No further
   event follows, and there is no polling refetch, so the row stuck for good
   and its Cancel could only ever miss.
2. **Cancelling the warmup destroyed a finished install.** The warmup used to
   run ahead of every piece of finalisation, and ``CancelledError`` is a
   ``BaseException``, so it slipped past the warmup's ``except Exception`` into
   ``_run_install``'s cancel handler and marked the item cancelled. 5.5 GB on
   disk, no DOWNLOAD_COMPLETE, no shortcut flip, game shows as not installed.

Note on (2): a test that counts effects *at the moment of cancellation* passes
on the broken code too, because the destructive part happens during the unwind.
Every assertion here is made after the install task has fully settled.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.types import InstallResult
from unifideck.core.types.events import Events
from unifideck.services.download import DownloadService
from unifideck.services.download.models import DownloadItem


def _service(tmp_path, monkeypatch, *, store_name="gog"):
    """A service whose one store always installs successfully."""
    bus = MagicMock()
    bus.emit = AsyncMock()
    store = MagicMock()
    store.install_game = AsyncMock(
        return_value=InstallResult(
            success=True, install_path=str(tmp_path / "game"), game_id="g1",
        ),
    )
    registry = MagicMock()
    registry.get_store.return_value = store
    svc = DownloadService(
        bus, registry, str(tmp_path / "queue.json"), launcher_path="",
    )
    # ``build_installed_game`` reads the install dir off disk; the Game record
    # itself is not what these tests are about.
    monkeypatch.setattr(
        "unifideck.services.download.worker.build_installed_game",
        AsyncMock(return_value={"id": f"{store_name}:g1"}),
    )
    return svc, bus


def _item(tmp_path, store="gog"):
    return DownloadItem(
        store=store, game_id="g1", install_path=str(tmp_path / "game"),
    )


def _emitted(bus):
    return [call.args[0] for call in bus.emit.await_args_list]


async def _run_and_settle(svc, item):
    """Dispatch one install the way the worker loop does, then drain it."""
    key = f"{item.store}:{item.game_id}"
    svc._running[key] = item
    task = asyncio.create_task(svc._run_install(item))
    svc._running_tasks[key] = task
    return key, task


@pytest.mark.asyncio
async def test_cancel_during_warmup_keeps_the_install(tmp_path, monkeypatch):
    """The reported orphan bug: the install must survive its warmup dying."""
    svc, bus = _service(tmp_path, monkeypatch)
    warmup_started = asyncio.Event()

    async def _slow_warmup(_item):
        warmup_started.set()
        await asyncio.sleep(30)

    svc.set_prefix_warmup(_slow_warmup)
    item = _item(tmp_path)

    key, task = await _run_and_settle(svc, item)
    await asyncio.wait_for(warmup_started.wait(), 5)

    result = await svc.cancel("gog", "g1")

    assert result.success is True
    # Let the install task finish unwinding before judging anything: the
    # destructive version of this bug did its damage here, not above.
    await asyncio.wait_for(task, 5)

    assert item.status == "complete", "a finished install was marked cancelled"
    assert Events.DOWNLOAD_COMPLETE in _emitted(bus)
    assert Events.DOWNLOAD_CANCELLED not in _emitted(bus)
    # And the row is gone, so nothing is left for the user to cancel again.
    assert svc.get_queue()["current"] is None
    assert svc._warmup_runner.is_running(key) is False


@pytest.mark.asyncio
async def test_terminal_item_is_never_the_current_download(tmp_path, monkeypatch):
    """``get_queue`` must hide a finished item still sitting in ``_running``.

    Reproduces the exact window the frontend refetched into: the assertion runs
    from inside the ``on_complete`` callback, which is what ran for 6.5 s on
    the Epic install while the item was both terminal and still in ``_running``.
    """
    svc, _bus = _service(tmp_path, monkeypatch)
    item = _item(tmp_path)
    seen = {}

    async def _on_complete(_item):
        snapshot = svc.get_queue()
        seen["current"] = snapshot["current"]
        seen["state"] = snapshot["state"]
        seen["still_running"] = f"{_item.store}:{_item.game_id}" in svc._running

    svc.set_on_complete_callback(_on_complete)

    _key, task = await _run_and_settle(svc, item)
    await asyncio.wait_for(task, 5)

    assert seen["still_running"] is True, "window not reproduced"
    assert seen["current"] is None, "a completed install was reported as current"
    assert seen["state"] == "idle"


@pytest.mark.asyncio
async def test_cancel_clears_an_orphaned_row(tmp_path, monkeypatch):
    """A visible row with no live task is cleared, not answered not_found."""
    svc, bus = _service(tmp_path, monkeypatch)
    item = _item(tmp_path)
    svc._running["gog:g1"] = item  # no task: the state the stuck row left behind

    result = await svc.cancel("gog", "g1")

    assert result.success is True
    assert item.status == "cancelled"
    assert Events.DOWNLOAD_CANCELLED in _emitted(bus)
    assert svc.get_queue()["current"] is None
    assert "gog:g1" not in svc._running


@pytest.mark.asyncio
async def test_cancel_during_finalisation_does_not_undo_a_finished_install(
    tmp_path, monkeypatch,
):
    """A cancel landing in the finalisation window must be a no-op.

    This is the same 6.5 s window the stale row was cached in, so a frontend
    holding that row is exactly who would send this. The item is terminal but
    its task is still alive (inside ``on_complete``), so without the
    already-finished guard ``_cancel_running`` would kill the task and flip a
    completed install — DOWNLOAD_COMPLETE already emitted — to ``cancelled``.
    """
    svc, bus = _service(tmp_path, monkeypatch)
    item = _item(tmp_path)
    outcome = {}

    async def _on_complete(_item):
        # Mid-finalisation: terminal status, task still running.
        outcome["result"] = await svc.cancel("gog", "g1")

    svc.set_on_complete_callback(_on_complete)

    _key, task = await _run_and_settle(svc, item)
    await asyncio.wait_for(task, 5)

    assert outcome["result"].success is True
    assert item.status == "complete", "a cancel undid a finished install"
    assert Events.DOWNLOAD_CANCELLED not in _emitted(bus)


@pytest.mark.asyncio
async def test_cancel_with_nothing_in_flight_still_reports_not_found(
    tmp_path, monkeypatch,
):
    """The orphan fallback must not turn every stray cancel into a success."""
    svc, _bus = _service(tmp_path, monkeypatch)

    result = await svc.cancel("gog", "nope")

    assert result.success is False
    assert result.error == "not_found"


@pytest.mark.asyncio
async def test_outer_cancellation_is_not_absorbed(tmp_path, monkeypatch):
    """Shutdown still works, and the install survives it anyway.

    Two things at once, because they are the same scenario.

    ``PrefixWarmupRunner.run`` absorbs a ``CancelledError`` it was told to
    expect. Cancelling the *install* task during a warmup is a different thing
    (plugin teardown, or a cancel that reached ``_running_tasks``) and has to
    propagate, or ``stop()`` could never interrupt a 600 s warmup. So the item
    does end up ``cancelled`` here.

    But DOWNLOAD_COMPLETE must already have gone out, and that is what the
    finalise-before-warmup ordering buys. This is the assertion the ordering is
    accountable to: with the warmup running first, a teardown mid-setup left no
    DOWNLOAD_COMPLETE, no shortcut flip and no ``games.map`` entry for a game
    that was completely installed. Note the runner's cancel-absorption alone
    does *not* cover this case — nothing absorbs an outer cancel, by design.
    """
    svc, bus = _service(tmp_path, monkeypatch)
    warmup_started = asyncio.Event()

    async def _slow_warmup(_item):
        warmup_started.set()
        await asyncio.sleep(30)

    svc.set_prefix_warmup(_slow_warmup)
    item = _item(tmp_path)

    _key, task = await _run_and_settle(svc, item)
    await asyncio.wait_for(warmup_started.wait(), 5)

    task.cancel()  # NOT via svc.cancel(): no abandon flag is recorded
    with pytest.raises(asyncio.CancelledError):
        await task

    assert item.status == "cancelled"
    assert Events.DOWNLOAD_COMPLETE in _emitted(bus), (
        "teardown during setup discarded a finished install's bookkeeping"
    )


@pytest.mark.asyncio
async def test_wrapper_store_finalises_without_a_warmup(tmp_path, monkeypatch):
    """A wrapper store never reaches "preparing", and its row still clears.

    The emit-before-cleanup race is store-agnostic: ``download_history.json``
    on the reporting device holds Ubisoft rows at ``status=complete`` with
    ``phase=manual``, so wrapper stores could strand the same stale row with a
    different label.
    """
    svc, bus = _service(tmp_path, monkeypatch, store_name="ubisoft")
    svc.set_prefix_warmup(AsyncMock())
    item = _item(tmp_path, store="ubisoft")

    _key, task = await _run_and_settle(svc, item)
    await asyncio.wait_for(task, 5)

    assert item.status == "complete"
    assert item.download_phase != "preparing"
    assert Events.DOWNLOAD_COMPLETE in _emitted(bus)
    assert svc.get_queue()["current"] is None


@pytest.mark.asyncio
async def test_completed_item_keeps_a_terminal_phase_in_history(
    tmp_path, monkeypatch,
):
    """Every finished row on the device still read ``phase=preparing``.

    The phase was never reset, so a stale snapshot of a finished install
    rendered as the indeterminate "SETTING UP GAME..." bar. Failure and cancel
    paths deliberately keep their phase — there it says which step died.
    """
    svc, _bus = _service(tmp_path, monkeypatch)
    svc.set_prefix_warmup(AsyncMock())
    item = _item(tmp_path)

    _key, task = await _run_and_settle(svc, item)
    await asyncio.wait_for(task, 5)

    finished = svc.get_queue()["finished"]
    assert [row["download_phase"] for row in finished] == ["complete"]
