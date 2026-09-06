"""Tests for the install-time prefix-warmup hook on the download worker.

Covers ``_WorkerMixin._run_prefix_warmup`` gating (which stores trigger it),
the "preparing" phase + DOWNLOAD_STARTED re-emit, best-effort failure
handling, and the ``make_prefix_warmup`` factory pass-through.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.types.events import Events
from unifideck.services.download import DownloadService
from unifideck.services.download import prefix_warmup as warmup_mod
from unifideck.services.download.models import DownloadItem


def _service(tmp_path):
    bus = MagicMock()
    bus.emit = AsyncMock()
    registry = MagicMock()
    svc = DownloadService(
        bus, registry, str(tmp_path / "queue.json"), launcher_path="",
    )
    return svc, bus


def _item(store):
    return DownloadItem(
        store=store, game_id="g1", install_path="/data/install/g1",
    )


@pytest.mark.parametrize("store", ["gog", "epic", "amazon"])
@pytest.mark.asyncio
async def test_warmup_runs_for_prefix_stores(tmp_path, store):
    svc, bus = _service(tmp_path)
    hook = AsyncMock()
    svc.set_prefix_warmup(hook)
    item = _item(store)

    await svc._run_prefix_warmup(item)

    hook.assert_awaited_once_with(item)
    assert item.download_phase == "preparing"
    # The phase change is surfaced by re-emitting DOWNLOAD_STARTED so the
    # frontend refetches the queue and the row picks up the new phase.
    assert bus.emit.await_args.args[0] == Events.DOWNLOAD_STARTED


@pytest.mark.parametrize("store", ["ubisoft"])
@pytest.mark.asyncio
async def test_warmup_skipped_for_excluded_stores(tmp_path, store):
    """Skipped for a wrapper store: UPC bootstraps its own prefix.

    ``microsoft`` used to be listed here too, as a second store-name arm.
    It is gone, and the parametrize is kept (rather than inlined) because
    the rule is a category — ``uses_manual_download_phase`` — not a name,
    and a future wrapper store belongs in this list.
    """
    svc, bus = _service(tmp_path)
    hook = AsyncMock()
    svc.set_prefix_warmup(hook)
    item = _item(store)

    await svc._run_prefix_warmup(item)

    hook.assert_not_awaited()
    assert item.download_phase == "downloading"  # unchanged default
    bus.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_store_never_reaches_warmup_because_install_is_refused(
    tmp_path,
):
    """Why the ``microsoft`` arm above could be dropped.

    Warmup runs from ``_on_install_success`` only, and the cloud-only
    store now refuses every install (audit §3.5, register item 11) — so
    the arm guarded a path that cannot be taken. Asserting the *reason*
    beats re-asserting the defensive branch: a branch test passes even if
    the store starts reporting phantom successes, which is the bug the
    refusal fixed.
    """
    from unifideck.stores.microsoft.microsoft_store import MicrosoftStore

    bus = MagicMock()
    bus.emit = AsyncMock()
    cache = MagicMock()
    cache.get.return_value = None
    store = MicrosoftStore(bus, cache, plugin_dir="/plugin")

    result = await store.install_game("9NXR0000TEST")

    assert result.success is False
    assert result.error_code == "not_supported"


@pytest.mark.asyncio
async def test_warmup_noop_when_unwired(tmp_path):
    svc, bus = _service(tmp_path)  # set_prefix_warmup never called
    item = _item("gog")

    await svc._run_prefix_warmup(item)

    assert item.download_phase == "downloading"
    bus.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_warmup_failure_is_best_effort(tmp_path):
    svc, _bus = _service(tmp_path)
    svc.set_prefix_warmup(AsyncMock(side_effect=RuntimeError("boom")))
    item = _item("gog")

    # Must not raise — a warmup failure can never break install completion.
    await svc._run_prefix_warmup(item)
    assert item.download_phase == "preparing"


@pytest.mark.asyncio
async def test_make_prefix_warmup_passes_item_fields(monkeypatch):
    captured = {}

    async def _fake(store, game_id, install_path, *, cloud_svc=None):
        captured.update(
            store=store, game_id=game_id,
            install_path=install_path, cloud_svc=cloud_svc,
        )

    monkeypatch.setattr(warmup_mod, "warmup_install_prefix", _fake)
    sentinel_cloud = object()
    hook = warmup_mod.make_prefix_warmup(sentinel_cloud)

    await hook(_item("gog"))

    assert captured == {
        "store": "gog", "game_id": "g1",
        "install_path": "/data/install/g1", "cloud_svc": sentinel_cloud,
    }
