"""Luna page settings: streaming quality + language, config, RPC, kiosk write.

Luna keeps both in ``localStorage`` (``luna_video_quality_setting`` with
LIMITED / STANDARD / BEST, and ``locale_override`` in ``de_DE`` style) and
reads them once per page load, so a changed value is written and the page
reloaded once (verified on a Steam Deck).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import unifideck.stores.luna.luna_page_settings as ps
from unifideck.rpc.mixins.ui import UIRPCMixin
from unifideck.stores.luna.luna_api.regions import resolve_region
from unifideck.stores.luna.luna_config import (
    VIDEO_QUALITY_KEY,
    LunaConfig,
    normalize_video_quality,
)
from unifideck.stores.luna.luna_locale import (
    LUNA_LOCALE_STORAGE_KEY,
    luna_locale_for,
)
from unifideck.stores.luna.luna_page_settings import (
    LUNA_QUALITY_STORAGE_KEY,
    LunaPageSettingsError,
    apply_page_settings,
    build_apply_expression,
    page_settings_for,
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
    """A page holding ``stored`` in localStorage; records CDP calls."""

    def __init__(self, stored: dict[str, str], *, throws: bool = False) -> None:
        self.stored = dict(stored)
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
        want = json.loads(expr.split("const want = ", 1)[1].split("; let changed", 1)[0])
        changed = any(self.stored.get(k) != v for k, v in want.items())
        self.stored.update(want)
        return {"result": {"value": changed}}

    def attached_targets(self, idle_timeout: float | None = 2.0) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ps, "_RELOAD_SETTLE_S", 0)


# ── quality ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("BEST", "BEST"), (" limited ", "LIMITED"), ("", ""), ("auto", ""),
     ("ULTRA", ""), (None, "")],
)
def test_normalize_video_quality(raw: object, expected: str) -> None:
    assert normalize_video_quality(raw) == expected


def test_config_reads_video_quality_under_stores_luna(monkeypatch: pytest.MonkeyPatch) -> None:
    import unifideck.stores.luna.luna_api.regions as regions

    monkeypatch.setattr(regions, "detect_region", lambda _p: None)
    cfg = LunaConfig.from_config_manager(_Config({VIDEO_QUALITY_KEY: "standard"}))
    assert VIDEO_QUALITY_KEY == "stores.luna.video_quality"
    assert cfg.video_quality == "STANDARD"


# ── language mapping ────────────────────────────────────────────
@pytest.mark.parametrize(
    ("ui", "host", "expected"),
    [
        ("de-DE", "luna.amazon.de", "de_DE"),
        ("en-GB", "luna.amazon.de", "en_GB"),
        ("en", "luna.amazon.co.uk", "en_GB"),      # same language: the site's variant
        ("fr-BE", "luna.amazon.ca", "fr_CA"),      # same language: the country's variant
        ("es-MX", "luna.amazon.com", "es_US"),
        ("fr-FR", "luna.amazon.de", "fr_FR"),
        ("pt-BR", "luna.amazon.es", "pt_PT"),
        ("ja-JP", "luna.amazon.de", "de_DE"),      # not offered: site default
        ("", "luna.amazon.com", "en_US"),
        (None, None, "en_US"),
    ],
)
def test_luna_locale_for(ui: str | None, host: str | None, expected: str) -> None:
    region = resolve_region(host) if host else None
    assert luna_locale_for(ui, region) == expected


# ── what gets written ───────────────────────────────────────────
def test_page_settings_skip_empty_values() -> None:
    cfg = SimpleNamespace(video_quality="", locale="de_DE")
    assert page_settings_for(cfg) == {LUNA_LOCALE_STORAGE_KEY: "de_DE"}  # type: ignore[arg-type]
    cfg = SimpleNamespace(video_quality="BEST", locale="")
    assert page_settings_for(cfg) == {LUNA_QUALITY_STORAGE_KEY: "BEST"}  # type: ignore[arg-type]


def test_storage_keys_are_lunas_own() -> None:
    assert LUNA_QUALITY_STORAGE_KEY == "luna_video_quality_setting"
    assert LUNA_LOCALE_STORAGE_KEY == "locale_override"
    expr = build_apply_expression({LUNA_QUALITY_STORAGE_KEY: "BEST"})
    assert '"luna_video_quality_setting": "BEST"' in expr


async def test_changed_value_is_written_and_the_page_reloaded_once() -> None:
    page = _Page({LUNA_QUALITY_STORAGE_KEY: "LIMITED", LUNA_LOCALE_STORAGE_KEY: "en_US"})
    want = {LUNA_QUALITY_STORAGE_KEY: "BEST", LUNA_LOCALE_STORAGE_KEY: "de_DE"}
    assert await run_apply(page, "s", want) is True
    assert page.stored == want
    assert page.calls == ["Runtime.evaluate", "Page.reload"]


async def test_unchanged_values_need_no_reload() -> None:
    want = {LUNA_QUALITY_STORAGE_KEY: "BEST", LUNA_LOCALE_STORAGE_KEY: "de_DE"}
    page = _Page(want)
    assert await run_apply(page, "s", want) is False
    assert "Page.reload" not in page.calls


async def test_unwritable_storage_raises() -> None:
    with pytest.raises(LunaPageSettingsError):
        await run_apply(_Page({}, throws=True), "s", {LUNA_QUALITY_STORAGE_KEY: "BEST"})


async def test_nothing_to_apply_touches_nothing() -> None:
    cdp = SimpleNamespace(get_browser_ws_url=lambda: pytest.fail("no CDP needed"))
    assert await apply_page_settings(cdp, {}) is False  # type: ignore[arg-type]


# ── RPC ─────────────────────────────────────────────────────────
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
