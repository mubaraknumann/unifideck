"""Tests for the generic seams the itch.io store needed, fixed once for all stores.

* every store's ``shutdown`` runs on plugin unload (Microsoft's token poll used
  to be the one hard-coded case);
* a nested native launch target runs from its own folder;
* a native install gets no Proton prefix warmup;
* the page-content auth capture can evaluate an expression other than
  ``innerText``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.auth import browser_content
from unifideck.auth.browser_types import INNER_TEXT_EXPRESSION, ContentCapture
from unifideck.bootstrap.teardown import unload_plugin
from unifideck.launcher.types.context import LaunchContext, is_windows_executable
from unifideck.services.download.models import DownloadItem
from unifideck.services.download.worker_helpers import prefix_warmup_supported
from unifideck.services.launcher.helpers import native_cwd


# ── teardown ───────────────────────────────────────────────────────────
async def test_unload_calls_shutdown_on_every_store_even_after_a_failure() -> None:
    good = SimpleNamespace(store_name="itch", shutdown=AsyncMock())
    bad = SimpleNamespace(store_name="microsoft", shutdown=AsyncMock(side_effect=RuntimeError))
    after = SimpleNamespace(store_name="epic", shutdown=AsyncMock())
    registry = MagicMock()
    registry.all.return_value = [bad, good, after]
    plugin = SimpleNamespace(registry=registry, services=None, dispatcher=None, bus=None)
    await unload_plugin(plugin)
    for store in (bad, good, after):
        store.shutdown.assert_awaited_once()


async def test_microsoft_shutdown_stops_its_token_poll() -> None:
    from unifideck.stores.microsoft.microsoft_store import MicrosoftStore

    store = MicrosoftStore.__new__(MicrosoftStore)
    store.stop_token_refresh_polling = AsyncMock()  # type: ignore[method-assign]
    await store.shutdown()
    store.stop_token_refresh_polling.assert_awaited_once()


# ── native launch cwd ──────────────────────────────────────────────────
def _ctx(exe: Path, work_dir: Path, store: str = "itch") -> LaunchContext:
    return LaunchContext(store=store, game_id="1", exe_path=exe, work_dir=work_dir,
                         plugin_dir=Path("/plugin"))


def test_nested_native_target_runs_from_its_own_folder(tmp_path: Path) -> None:
    exe = tmp_path / "fear-assessment" / "linux64" / "nw"
    assert native_cwd(_ctx(exe, tmp_path / "fear-assessment")) == exe.parent


def test_root_level_target_keeps_the_recorded_work_dir(tmp_path: Path) -> None:
    # GOG's start.sh sits at the install root: unchanged behaviour.
    exe = tmp_path / "game" / "start.sh"
    assert native_cwd(_ctx(exe, tmp_path / "game", store="gog")) == tmp_path / "game"


def test_unrelated_work_dir_is_kept(tmp_path: Path) -> None:
    exe = tmp_path / "a" / "bin" / "game"
    assert native_cwd(_ctx(exe, tmp_path / "elsewhere")) == tmp_path / "elsewhere"


def test_windows_executable_split() -> None:
    assert is_windows_executable("/x/Game.EXE") and is_windows_executable("run.bat")
    assert not is_windows_executable("/x/linux64/nw")
    assert not is_windows_executable("/x/HamptonCourt.sh")


# ── prefix warmup ──────────────────────────────────────────────────────
def _item(store: str, tmp_path: Path) -> DownloadItem:
    return DownloadItem(store=store, game_id="1", title="t", install_path=str(tmp_path))


@pytest.mark.parametrize(("exe", "warm"), [
    ("/g/linux64/nw", False),
    ("/g/HamptonCourt.sh", False),
    ("/g/HamptonCourt.exe", True),
    ("", True),  # unknown exe: keep the old behaviour
])
def test_warmup_follows_the_reported_exe(tmp_path: Path, exe: str, warm: bool) -> None:
    result = SimpleNamespace(metadata={"exe_path": exe})
    assert prefix_warmup_supported(_item("itch", tmp_path), result) is warm


def test_warmup_without_a_result_is_unchanged(tmp_path: Path) -> None:
    assert prefix_warmup_supported(_item("amazon", tmp_path)) is True


# ── content capture expression ─────────────────────────────────────────
async def test_content_capture_passes_its_expression_to_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_eval(ws_url: str, snippet: str, first: bool, expression: str = INNER_TEXT_EXPRESSION) -> str:
        seen["expression"] = expression
        return "K" * 40

    monkeypatch.setattr(browser_content, "cdp_eval_inner_text", fake_eval)
    capture = ContentCapture(trigger_url="example.test/keys", regex=r"^([A-Z]{40})$",
                             expression="document.querySelector('code').textContent")
    target = {"url": "https://example.test/keys", "webSocketDebuggerUrl": "ws://x"}
    result = await browser_content.try_content_fallback([target], capture, 0.0)
    assert result is not None and result.code == "K" * 40
    assert seen["expression"] == capture.expression


def test_default_capture_expression_is_inner_text() -> None:
    assert ContentCapture(trigger_url="a", regex="b").expression == INNER_TEXT_EXPRESSION
