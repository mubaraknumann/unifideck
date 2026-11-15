"""A fresh install is set up on the latest GE and gets it as its Steam setting.

On a Steam Deck Steam's global Steam Play default is Valve's Proton, which
ships no FFmpeg, so a 32-bit game's H.264 intro crashed it (2026-11-15). Steam's
per-game Compatibility setting stays the only switch: the frontend sets it for a
fresh install from ``item.proton_tool``; nothing is kept by the plugin. Updates,
existing games and wrapper stores are never touched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.core.types import InstallResult
from unifideck.launcher import proton as proton_pkg
from unifideck.launcher.proton import prefix_setup
from unifideck.launcher.types.context import LaunchContext
from unifideck.launcher.wrapper_stores import WRAPPER_STORES
from unifideck.services.download import worker_helpers
from unifideck.services.download.models import DownloadItem
from unifideck.services.download.worker import _WorkerMixin

_GE = (Path("/ge/GE-Proton11-12/proton"), "GE-Proton11-12")


def _item(store: str = "ea", *, update: bool = False) -> DownloadItem:
    item = DownloadItem(store=store, game_id="1", install_path="/g", title="G")
    item.is_update = update
    return item


@pytest.fixture
def latest_ge(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    picks = [_GE]
    monkeypatch.setattr(proton_pkg, "select_managed_ge_proton", lambda: picks[0])
    return picks


async def test_a_fresh_install_gets_the_latest_ge(latest_ge: Any) -> None:
    item = _item()
    await worker_helpers.pick_new_install_proton(item, gets_prefix_setup=True)
    assert item.proton_tool == "GE-Proton11-12"
    assert item.to_dict()["proton_tool"] == "GE-Proton11-12"


@pytest.mark.parametrize(("item", "gets_setup"), [
    (_item(update=True), True),
    (_item(), False),
    *[(_item(store), True) for store in sorted(WRAPPER_STORES)],
], ids=["update", "no-prefix-setup", *sorted(WRAPPER_STORES)])
async def test_updates_non_proton_games_and_wrapper_stores_are_left_alone(
    latest_ge: Any, item: DownloadItem, gets_setup: bool,
) -> None:
    await worker_helpers.pick_new_install_proton(item, gets_prefix_setup=gets_setup)
    assert item.proton_tool is None


async def test_no_ge_available_leaves_steams_setting_alone(latest_ge: list[tuple[Path, str]]) -> None:
    latest_ge[0] = (Path("/x/proton"), "proton_experimental")  # the offline fallback
    item = _item()
    await worker_helpers.pick_new_install_proton(item, gets_prefix_setup=True)
    assert item.proton_tool is None


def _ctx(tool: str | None) -> LaunchContext:
    return LaunchContext(
        store="ea", game_id="1", exe_path=Path("/g"), work_dir=Path("/g"), plugin_dir=Path("/p"), proton_tool=tool,
    )


def test_the_prefix_is_set_up_on_the_chosen_proton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proton_pkg, "resolve_proton_path", lambda tool: Path(f"/tools/{tool}/proton"))
    selected = MagicMock(return_value=(Path("/x/proton"), "proton_experimental"))

    assert prefix_setup._chosen_or_selected(_ctx("GE-Proton11-12"), selected) == (
        Path("/tools/GE-Proton11-12/proton"), "GE-Proton11-12",
    )
    selected.assert_not_called()
    assert prefix_setup._chosen_or_selected(_ctx(None), selected)[1] == "proton_experimental"


def test_a_missing_chosen_proton_falls_back_to_the_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proton_pkg, "resolve_proton_path", lambda tool: None)
    selected = MagicMock(return_value=(Path("/x/proton"), "proton_experimental"))
    assert prefix_setup._chosen_or_selected(_ctx("GE-Proton99-1"), selected)[1] == "proton_experimental"


class _Worker(_WorkerMixin):
    def __init__(self) -> None:
        self._bus = MagicMock(emit=AsyncMock())  # type: ignore[assignment]
        self.tool_at_finalise: str | None = "unset"

    def _warmup_applies(self, item: Any, result: Any = None) -> bool:
        return True

    async def _finalise_install(self, item: Any, *args: Any) -> None:
        self.tool_at_finalise = item.proton_tool  # DOWNLOAD_COMPLETE carries the item from here

    async def _run_prefix_warmup(self, item: Any, key: str) -> None:
        return None

    def _mark_item_complete(self, item: Any) -> None:
        return None

    async def _emit_queue_refresh(self, item: Any) -> None:
        return None


async def test_the_choice_is_on_the_item_before_the_completion_event(latest_ge: Any) -> None:
    worker = _Worker()
    item = _item()
    await worker._on_install_success(  # type: ignore[call-arg]
        item, InstallResult(success=True, store="ea", game_id="1"), MagicMock(), "ea:1",
    )
    assert worker.tool_at_finalise == "GE-Proton11-12"
