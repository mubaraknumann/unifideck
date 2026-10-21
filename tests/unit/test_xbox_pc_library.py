"""Tests for ``XboxPcLibrary.compose``: which PC rows a sync returns.

A list from ``get_library`` is authoritative (reconcile deletes every
Microsoft shortcut missing from it), so the cases that matter are the ones
that must NOT drop rows: a failed or partial read, a spent budget, a
signed-out CLI, the feature turned off with games installed, and an
unmounted SD card.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.test_xbox_pc_installed import make_game
from unifideck.core.types import Events, Game, GameTag
from unifideck.stores.microsoft.pc_library import library as library_mod
from unifideck.stores.microsoft.pc_library.entitlements import parse_rfc3339
from unifideck.stores.microsoft.pc_library.library import XboxPcLibrary, has_pc_rows
from unifideck.stores.microsoft.pc_library.snapshot import CACHE_NAME

FIXTURE = Path(__file__).parent.parent / "fixtures" / "microsoft" / "xbox_pc_library_IN.json"
_CHECKED = parse_rfc3339("2026-10-20T18:51:54.508009681+00:00")
assert _CHECKED is not None
NOW = _CHECKED + 10
BALATRO = "9PK087LNGJC5"
OWNED = "9MVQN16MCT10"
UBISOFT = "9MTP0GJ0Q3MG"
BUNDLE = "9PB4ZWV7S2MG"
BUNDLE_PART = "9MVJ1KSRJQJQ"


def _product(pid: str, cid: str, title: str) -> dict[str, Any]:
    return {"ProductId": pid, "ProductType": "Game", "LocalizedProperties": [{"ProductTitle": title}],
            "DisplaySkuAvailabilities": [{"Sku": {"Properties": {"Packages": [
                {"ContentId": cid, "PackageFormat": "MSIXVC", "MaxDownloadSizeInBytes": 900,
                 "PlatformDependencies": [{"PlatformName": "Windows.Desktop"}]}]}}}]}


CATALOG = {
    BALATRO: _product(BALATRO, "1760268b-59d2-488b-8f68-b864a82b23be", "Balatro"),
    OWNED: _product(OWNED, "21111111-1111-4111-8111-111111111111", "Owned Game"),
    UBISOFT: _product(UBISOFT, "31111111-1111-4111-8111-111111111111", "Ubisoft Game"),
    BUNDLE_PART: _product(BUNDLE_PART, "41111111-1111-4111-8111-111111111111", "Bundle Part"),
    BUNDLE: {"ProductId": BUNDLE, "ProductType": "Game", "LocalizedProperties": [{"ProductTitle": "Bundle"}],
             "DisplaySkuAvailabilities": [{"Sku": {"Properties": {"BundledSkus": [{"BigId": BUNDLE_PART}]}}}]},
}


class _Cache:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get(self, cache: str, key: str) -> Any:
        return json.loads(json.dumps(self.data.get((cache, key))))

    def set(self, cache: str, key: str, value: Any) -> None:
        self.data[(cache, key)] = json.loads(json.dumps(value))

    def delete(self, cache: str, key: str) -> None:
        self.data.pop((cache, key), None)


class _Bus:
    def __init__(self) -> None:
        self.skipped: list[str] = []

    async def emit(self, event: Any, **kw: Any) -> None:
        assert event is Events.SYNC_SKIPPED and kw["store"] == "microsoft"
        self.skipped.append(kw["reason"])


class _Facade:
    def __init__(self) -> None:
        self.on = True
        self.counts: list[dict[str, Any]] = []

    def enabled(self) -> bool:
        return self.on

    def set_library_counts(self, counts: dict[str, Any]) -> None:
        self.counts.append(counts)


class _Cli:
    def __init__(self) -> None:
        self.status = "ok"
        self.stdout = FIXTURE.read_text()
        self.calls = 0
        self.hang = False
        self.cancelled = False

    async def library(self, market: str) -> Any:
        self.calls += 1
        assert market == "IN"
        if self.hang:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return SimpleNamespace(status=self.status, stdout=self.stdout if self.status == "ok" else "", tail="")


class _Catalog:
    def __init__(self) -> None:
        self.failing: set[str] = set()
        self.calls = 0
        self.asked: list[str] = []

    async def __call__(self, ids: list[str], market: str, *, languages: str) -> Any:
        self.calls += 1
        self.asked.extend(ids)
        found = {i: CATALOG[i] for i in ids if i in CATALOG and i not in self.failing}
        return found, [i for i in ids if i in self.failing]


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    ns = SimpleNamespace(cache=_Cache(), bus=_Bus(), facade=_Facade(), cli=_Cli(),
                         catalog=_Catalog(), tmp=tmp_path)
    ns.make = lambda: XboxPcLibrary(None, ns.facade, ns.cli, cache=ns.cache, bus=ns.bus,
                                    fetch_products=ns.catalog, clock=lambda: NOW)
    ns.lib = ns.make()
    return ns


def _xcloud(pid: str, title: str = "Game") -> Game:
    return Game(app_id=0, store="microsoft", store_game_id=pid, title=title,
                tags=[GameTag.XCLOUD, GameTag.BROWSER], metadata={"browser_url": "u", "game_pass": True})


def _by_id(rows: list[Game]) -> dict[str, Game]:
    return {g.store_game_id: g for g in rows}


async def test_clean_read_lists_merged_pc_only_and_not_supported_rows(env: SimpleNamespace) -> None:
    rows = _by_id(await env.lib.compose([_xcloud("9pk087lngjc5", "Balatro")], market="IN"))
    balatro = rows["9pk087lngjc5"]
    assert balatro.tags == [GameTag.XCLOUD, GameTag.BROWSER, GameTag.XBOX_PC]
    assert balatro.metadata["xbox_pc"]["state"] == "ready"
    assert balatro.metadata["xbox_pc"]["size_bytes"] == 900
    assert rows[OWNED].tags == [GameTag.XBOX_PC] and rows[OWNED].metadata["game_pass"] is False
    assert rows[UBISOFT].metadata["xbox_pc"]["state"] == "not_supported"
    assert rows[UBISOFT].metadata["xbox_pc"]["reason"] == "ubisoft_plus"
    assert "game_pass" not in rows[UBISOFT].metadata
    # The bundle and its part both target the part: one row, owned.
    assert rows[BUNDLE_PART].metadata["xbox_pc"]["access"] == "owned"
    assert BUNDLE not in rows
    assert env.bus.skipped == []
    assert env.facade.counts[-1]["not_supported"] == 1 and env.facade.counts[-1]["stale"] is False
    assert has_pc_rows(list(rows.values()))


async def test_feature_off_without_installs_is_todays_library(env: SimpleNamespace) -> None:
    env.facade.on = False
    games = [_xcloud("9NBLGGH4PNC7")]
    assert await env.lib.compose(games, market="IN") == games
    assert env.cli.calls == 0
    assert env.lib.start_read("IN") is None


async def test_feature_off_keeps_installed_games_playable(env: SimpleNamespace) -> None:
    await env.lib.compose([_xcloud("9pk087lngjc5")], market="IN")
    record = make_game(env.tmp / "games", BALATRO)
    env.facade.on = False
    rows = _by_id(await env.lib.compose([_xcloud("9pk087lngjc5")], market="IN"))
    balatro = rows["9pk087lngjc5"]
    assert GameTag.XBOX_PC in balatro.tags and balatro.installed
    assert balatro.exe_path == record["exe"] and balatro.install_path == record["installRoot"]
    assert OWNED not in rows  # not installed, so dropped with the feature off
    assert env.cli.calls == 1


async def test_signed_out_keeps_the_last_list_and_toasts_once(env: SimpleNamespace) -> None:
    first = await env.lib.compose([], market="IN")
    env.cli.status = "signed_out"
    for _ in range(2):
        again = await env.lib.compose([], market="IN")
        assert {g.store_game_id for g in again} == {g.store_game_id for g in first}
    assert env.bus.skipped == ["xbox_pc_signin_needed"]
    assert env.facade.counts[-1]["stale"] is True


async def test_the_store_pc_only_notice_is_sent_once(env: SimpleNamespace) -> None:
    for _ in range(2):
        await env.lib.notify_once("xbox_pc_only")
    assert env.bus.skipped == ["xbox_pc_only"]


async def test_a_running_game_keeps_the_list_without_a_toast(env: SimpleNamespace) -> None:
    await env.lib.compose([], market="IN")
    env.cli.status = "busy"
    assert has_pc_rows(await env.lib.compose([], market="IN"))
    assert env.bus.skipped == []


async def test_a_good_read_resets_the_notice(env: SimpleNamespace) -> None:
    env.cli.status = "failed"
    await env.lib.compose([], market="IN")
    env.cli.status = "ok"
    await env.lib.compose([], market="IN")
    env.cli.status = "failed"
    await env.lib.compose([], market="IN")
    assert env.bus.skipped == ["xbox_pc_list_kept", "xbox_pc_list_kept"]


async def test_spent_budget_keeps_the_last_list_and_kills_the_read(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await env.lib.compose([], market="IN")
    monkeypatch.setattr(library_mod, "REFRESH_BUDGET_S", 0.05)
    env.cli.hang = True
    pending = env.lib.start_read("IN")
    rows = await env.lib.compose([], market="IN", pending_read=pending)
    assert {g.store_game_id for g in rows} == {g.store_game_id for g in first}
    assert env.cli.cancelled and pending is not None and pending.cancelled()
    assert env.bus.skipped == ["xbox_pc_list_kept"]


async def test_the_budget_counts_from_the_start_of_the_library_read(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Time spent on the xCloud reads comes out of the same budget."""
    first = await env.lib.compose([], market="IN")
    monkeypatch.setattr(library_mod, "REFRESH_BUDGET_S", 0.3)
    started = env.lib.now()
    await asyncio.sleep(0.3)  # the gate, the session and the xCloud catalog
    env.cli.hang = True
    rows = await env.lib.compose([], market="IN", pending_read=env.lib.start_read("IN"), started=started)
    assert {g.store_game_id for g in rows} == {g.store_game_id for g in first}
    assert env.bus.skipped == ["xbox_pc_list_kept"]


async def test_zero_rows_after_a_good_list_is_an_anomaly(env: SimpleNamespace) -> None:
    first = await env.lib.compose([], market="IN")
    data = json.loads(env.cli.stdout)
    data["entitlements"] = []
    env.cli.stdout = json.dumps(data)
    rows = await env.lib.compose([], market="IN")
    assert len(rows) == len(first)
    assert env.bus.skipped == ["xbox_pc_list_kept"]


async def test_a_failed_catalog_batch_reuses_the_last_title(env: SimpleNamespace) -> None:
    await env.lib.compose([], market="IN")
    # A fresh library on the same cache, with the plan cache gone, and the
    # batch holding the owned game failing.
    for key in [k for k in env.cache.data if k[1].startswith("plans:")]:
        del env.cache.data[key]
    env.catalog.failing = {OWNED}
    rows = _by_id(await env.make().compose([], market="IN"))
    assert rows[OWNED].metadata["xbox_pc"]["state"] == "ready"
    assert env.facade.counts[-1]["stale"] is True
    assert env.bus.skipped == []


async def test_answered_plans_are_cached_and_absent_ones_asked_again(env: SimpleNamespace) -> None:
    await env.lib.compose([], market="IN")
    env.catalog.asked = []
    await env.lib.compose([], market="IN")
    assert env.catalog.asked
    assert not set(env.catalog.asked) & set(CATALOG)


async def test_an_account_change_clears_the_old_list(env: SimpleNamespace) -> None:
    await env.lib.compose([], market="IN")
    data = json.loads(env.cli.stdout)
    data["account"] = "someone.else@example.invalid"
    data["entitlements"] = [e for e in data["entitlements"] if e["productId"] == BALATRO]
    env.cli.stdout = json.dumps(data)
    rows = await env.lib.compose([], market="IN")
    assert [g.store_game_id for g in rows] == [BALATRO]
    assert env.bus.skipped == ["xbox_pc_account_changed"]
    stored = env.cache.data[(CACHE_NAME, "library")]
    assert "someone.else" not in json.dumps(stored)


async def test_unmounted_card_keeps_the_row_as_not_installed(env: SimpleNamespace) -> None:
    await env.lib.compose([], market="IN")
    make_game(env.tmp / "sdcard", OWNED)
    assert _by_id(await env.lib.compose([], market="IN"))[OWNED].installed
    import shutil

    shutil.rmtree(env.tmp / "sdcard")
    row = _by_id(await env.lib.compose([], market="IN"))[OWNED]
    assert not row.installed and GameTag.XBOX_PC in row.tags


async def test_installed_game_whose_entitlement_left_keeps_its_row(env: SimpleNamespace) -> None:
    make_game(env.tmp / "games", "9NOTLISTED00")
    rows = _by_id(await env.lib.compose([], market="IN"))
    row = rows["9NOTLISTED00"]
    assert row.installed and row.metadata["xbox_pc"]["state"] == "license_not_confirmed"


async def test_unreadable_record_is_reported_once_and_keeps_a_row(env: SimpleNamespace) -> None:
    make_game(env.tmp / "games", BALATRO)
    from unifideck.launcher.xbox_pc.layout import installed_dir

    (installed_dir() / "9NOTLISTED00.json").write_text("{torn")
    for _ in range(2):
        rows = _by_id(await env.lib.compose([], market="IN"))
    assert "9NOTLISTED00" in rows and not rows["9NOTLISTED00"].installed
    assert env.bus.skipped == ["xbox_pc_installed_unreadable"]
    assert (installed_dir() / "9NOTLISTED00.json").read_text() == "{torn"


async def test_case_change_on_the_xcloud_side_keeps_the_tile(env: SimpleNamespace) -> None:
    await env.lib.compose([_xcloud("9pk087lngjc5")], market="IN")
    rows = _by_id(await env.lib.compose([_xcloud("9PK087LNGJC5")], market="IN"))
    assert "9pk087lngjc5" in rows


async def test_title_for_works_after_a_restart(env: SimpleNamespace) -> None:
    await env.lib.compose([_xcloud("9pk087lngjc5")], market="IN")
    fresh = env.make()
    meta = fresh.title_for("9pk087lngjc5")
    assert meta is not None and meta["product_id"] == BALATRO and meta["state"] == "ready"
    assert fresh.title_for(OWNED) is not None
    assert fresh.title_for("9NBLGGH4PNC7") is None


def test_market_fallback(env: SimpleNamespace) -> None:
    assert env.lib.resolve_market("in") == "IN"
    assert env.lib.resolve_market("bogus") == env.lib.fallback_market()
    assert len(env.lib.fallback_market()) == 2


async def test_store_hooks_read_the_record(env: SimpleNamespace) -> None:
    await env.lib.compose([_xcloud("9pk087lngjc5")], market="IN")
    assert env.lib.installed_exe("9pk087lngjc5") is None
    assert env.lib.size_for("9pk087lngjc5") == 900
    record = make_game(env.tmp / "games", BALATRO)
    assert env.lib.installed_exe("9pk087lngjc5") == record["exe"]
    assert env.lib.installed_path("9pk087lngjc5") == record["installRoot"]
    assert env.lib.record_for("9NBLGGH4PNC7") is None
