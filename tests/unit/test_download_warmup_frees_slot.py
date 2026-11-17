"""A game setting up its prefix must not hold up the next download.

The warmup used to run inside the install task while the item held its
``_running`` slot, so with ``max_concurrent=1`` the next queued game waited for
the whole prefix setup (up to 10 minutes) before it could start downloading.
"""
import asyncio
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.types import InstallResult
from unifideck.services.download import DownloadService
from unifideck.services.download.models import DownloadItem


def _service(tmp_path, monkeypatch):
    """A service whose store installs instantly; every warmup blocks on ``gate``."""
    bus = MagicMock()
    bus.emit = AsyncMock()
    # Set when a game's store install starts / its warmup starts.
    installs: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
    warmups: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
    # A game whose id is in ``slow`` keeps "downloading" until ``slow`` clears.
    slow: dict[str, asyncio.Event] = {}

    async def _install(game_id, *_args, **_kwargs):
        installs[game_id].set()
        if game_id in slow:
            await slow[game_id].wait()
        return InstallResult(
            success=True, install_path=str(tmp_path / game_id), game_id=game_id,
        )

    store = MagicMock()
    store.install_game = AsyncMock(side_effect=_install)
    registry = MagicMock()
    registry.get_store.return_value = store
    svc = DownloadService(
        bus, registry, str(tmp_path / "queue.json"), launcher_path="",
    )
    monkeypatch.setattr(
        "unifideck.services.download.worker.build_installed_game",
        AsyncMock(return_value={"id": "x"}),
    )
    monkeypatch.setattr(
        "unifideck.services.download.worker.pick_new_install_proton", AsyncMock(),
    )
    monkeypatch.setattr(
        "unifideck.services.download.worker.stale_installs.reconcile_for_install",
        lambda *_a: None,
    )
    gate = asyncio.Event()
    warming = asyncio.Event()

    async def _warmup(item):
        warming.set()
        warmups[item.game_id].set()
        await gate.wait()

    svc.set_prefix_warmup(_warmup)
    svc.slow = slow
    svc.warmups = warmups
    return svc, installs, gate, warming


def _queue(svc, tmp_path, store, game_id):
    svc._queue.append(
        DownloadItem(store=store, game_id=game_id, install_path=str(tmp_path / game_id)),
    )


async def _tick(svc):
    """One worker-loop iteration without the poll sleep."""
    items = await svc._pop_ready_items()
    if items:
        await svc._dispatch_items(items)
    return items


@pytest.mark.asyncio
async def test_next_game_downloads_while_previous_sets_up(tmp_path, monkeypatch):
    svc, installs, gate, warming = _service(tmp_path, monkeypatch)
    svc.slow["b"] = b_done = asyncio.Event()
    _queue(svc, tmp_path, "gog", "a")
    _queue(svc, tmp_path, "gog", "b")

    assert [i.game_id for i in await _tick(svc)] == ["a"]
    await asyncio.wait_for(warming.wait(), 5)

    started = await _tick(svc)

    assert [i.game_id for i in started] == ["b"], "B waited for A's prefix setup"
    await asyncio.wait_for(installs["b"].wait(), 5)
    queue = svc.get_queue()
    running = {i["game_id"]: i for i in queue["running"]}
    assert running["a"]["download_phase"] == "preparing", "A's setup row vanished"
    assert "b" in running
    assert queue["current"]["game_id"] == "b", "the downloading game is the headline"

    b_done.set()
    gate.set()
    await asyncio.wait_for(asyncio.gather(*list(svc._running_tasks.values())), 5)
    assert svc._warming == set()


@pytest.mark.asyncio
async def test_two_finished_games_still_set_up_one_at_a_time(tmp_path, monkeypatch):
    svc, _installs, gate, warming = _service(tmp_path, monkeypatch)
    _queue(svc, tmp_path, "gog", "a")
    _queue(svc, tmp_path, "gog", "b")

    # Signal when a game reaches the runner, i.e. has entered its setup phase,
    # even if it then waits for the runner's one-at-a-time slot.
    entered: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
    real_run = svc._warmup_runner.run

    async def _run(key, make_coro):
        entered[key].set()
        return await real_run(key, make_coro)

    monkeypatch.setattr(svc._warmup_runner, "run", _run)

    await _tick(svc)
    await asyncio.wait_for(warming.wait(), 5)
    await _tick(svc)
    # B finishes downloading and queues behind A's warmup.
    await asyncio.wait_for(entered["gog:b"].wait(), 5)
    assert "gog:b" in svc._warming

    assert svc._warmup_runner.is_running("gog:a")
    assert not svc._warmup_runner.is_running("gog:b")
    # Neither warming game holds a download slot.
    assert svc._download_slots_used() == 0

    gate.set()
    await asyncio.wait_for(asyncio.gather(*list(svc._running_tasks.values())), 5)


@pytest.mark.asyncio
async def test_wrapper_store_waits_for_running_setup(tmp_path, monkeypatch):
    """A vendor-client install shares the umu runtime with the warmup."""
    svc, _installs, gate, warming = _service(tmp_path, monkeypatch)
    _queue(svc, tmp_path, "gog", "a")
    _queue(svc, tmp_path, "ubisoft", "u")

    await _tick(svc)
    await asyncio.wait_for(warming.wait(), 5)

    assert await _tick(svc) == [], "a wrapper install started during a warmup"
    assert [i.game_id for i in svc._queue] == ["u"]

    gate.set()
    await asyncio.wait_for(asyncio.gather(*list(svc._running_tasks.values())), 5)
    assert svc._head_can_start() is True


@pytest.mark.asyncio
async def test_cancel_setup_leaves_next_download_alone(tmp_path, monkeypatch):
    svc, installs, gate, warming = _service(tmp_path, monkeypatch)
    _queue(svc, tmp_path, "gog", "a")
    _queue(svc, tmp_path, "gog", "b")

    await _tick(svc)
    await asyncio.wait_for(warming.wait(), 5)
    a_task = svc._running_tasks["gog:a"]
    a_item = svc._running["gog:a"]
    await _tick(svc)
    b_task = svc._running_tasks["gog:b"]

    result = await svc.cancel("gog", "a")
    await asyncio.wait_for(a_task, 5)

    assert result.success is True
    assert a_item.status == "complete", "cancelling setup undid the install"
    assert not b_task.cancelled()
    await asyncio.wait_for(installs["b"].wait(), 5)

    gate.set()
    await asyncio.wait_for(b_task, 5)
