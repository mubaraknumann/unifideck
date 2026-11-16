"""Luna streaming quality: config, RPC, and the kiosk write + reload.

Luna stores the quality in ``localStorage["luna_video_quality_setting"]``
(levels LIMITED / STANDARD / BEST in its web bundle) and reads it once per
page load, so a changed value is written and the page reloaded once.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import unifideck.stores.luna.luna_video_quality as vq
from unifideck.rpc.mixins.ui import UIRPCMixin
from unifideck.stores.luna.luna_config import (
    VIDEO_QUALITY_KEY,
    LunaConfig,
    normalize_video_quality,
)
from unifideck.stores.luna.luna_video_quality import (
    LUNA_QUALITY_STORAGE_KEY,
    LunaVideoQualityError,
    apply_video_quality,
    build_apply_expression,
    run_apply,
)


class _Config:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


class _Page:
    """A page whose stored quality is ``stored``; records CDP calls."""

    def __init__(self, stored: str | None, *, throws: bool = False) -> None:
        self.stored = stored
        self.throws = throws
        self.calls: list[str] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(method)
        if method != "Runtime.evaluate":
            return {}
        if self.throws:
            return {"exceptionDetails": {"text": "SecurityError"}}
        expr = (params or {})["expression"]
        wanted = next(q for q in ("LIMITED", "STANDARD", "BEST") if f'"{q}"' in expr)
        changed = self.stored != wanted
        self.stored = wanted
        return {"result": {"value": changed}}

    def attached_targets(self, idle_timeout: float | None = 2.0) -> Any:
        raise NotImplementedError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("BEST", "BEST"), (" limited ", "LIMITED"), ("", ""), ("auto", ""),
     ("ULTRA", ""), (None, "")],
)
def test_normalize_video_quality(raw: object, expected: str) -> None:
    assert normalize_video_quality(raw) == expected


def test_config_reads_video_quality_under_stores_luna() -> None:
    cfg = LunaConfig.from_config_manager(_Config({VIDEO_QUALITY_KEY: "standard"}))
    assert VIDEO_QUALITY_KEY == "stores.luna.video_quality"
    assert cfg.video_quality == "STANDARD"


def test_apply_expression_targets_lunas_own_key() -> None:
    expr = build_apply_expression("BEST")
    assert f'"{LUNA_QUALITY_STORAGE_KEY}"' in expr
    assert '"BEST"' in expr


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vq, "_RELOAD_SETTLE_S", 0)


async def test_changed_value_is_written_and_the_page_reloaded() -> None:
    page = _Page(stored="LIMITED")
    assert await run_apply(page, "s", "BEST") is True
    assert page.stored == "BEST"
    assert page.calls == ["Runtime.evaluate", "Page.reload"]


async def test_unchanged_value_needs_no_reload() -> None:
    page = _Page(stored="BEST")
    assert await run_apply(page, "s", "BEST") is False
    assert "Page.reload" not in page.calls


async def test_unwritable_storage_raises() -> None:
    with pytest.raises(LunaVideoQualityError):
        await run_apply(_Page(stored=None, throws=True), "s", "BEST")


async def test_auto_touches_nothing() -> None:
    cdp = SimpleNamespace(get_browser_ws_url=lambda: pytest.fail("no CDP for Auto"))
    assert await apply_video_quality(cdp, "") is False  # type: ignore[arg-type]


async def test_rpc_round_trip_normalizes_and_persists() -> None:
    mixin = UIRPCMixin.__new__(UIRPCMixin)
    mixin.config = _Config()  # type: ignore[attr-defined]
    assert (await mixin.get_luna_video_quality())["quality"] == ""
    saved = await mixin.set_luna_video_quality("best")
    assert saved == {"success": True, "quality": "BEST"}
    got = await mixin.get_luna_video_quality()
    assert got["quality"] == "BEST"
    assert got["options"] == ["LIMITED", "STANDARD", "BEST"]
    assert (await mixin.set_luna_video_quality("nonsense"))["quality"] == ""
