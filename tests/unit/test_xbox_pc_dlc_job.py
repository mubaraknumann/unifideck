"""DLC inside the Xbox PC install and Update jobs (``XboxPcStoreHooks`` + ``DlcJob``).

- Install: the game, then its first DLC map (small selected, large only
  when ticked in the picker), then the DLC, all in one job.
- Update: with only DLC pending, the game is not streamed again.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _xbox_pc_fakes import PID, FakeBus, FakeCli, FakeXboxPc, plan, title_info, write_game

from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.launcher.xbox_pc.dlc_records import dlc_entries, pending_dlc
from unifideck.launcher.xbox_pc.install_records import read_install_record, write_install_record
from unifideck.stores.microsoft.pc_library.dlc import LARGE_DLC_BYTES, DlcInfo
from unifideck.stores.microsoft.xodus import install_steps
from unifideck.stores.microsoft.xodus.dlc_choice import apply_choice, view_for
from unifideck.stores.microsoft.xodus.dlc_job import initial_dlc, read_choice, save_choice
from unifideck.stores.microsoft.xodus.store_hooks import XboxPcStoreHooks
from unifideck.stores.shared import cli_install_helpers as helpers

SMALL = "9NB77CW5J1TT"
LARGE = "9N77PDNB7CWZ"
CIDS = {SMALL: "bfedb5f5-cd3c-40b7-9b43-f169b55cbc78", LARGE: "31c01c74-1cff-4912-a66b-5422714fc29f"}
OWNED = [
    DlcInfo(product_id=SMALL, parent=PID, title="Small pack", size_bytes=16_000_000),
    DlcInfo(product_id=LARGE, parent=PID, title="Big expansion", size_bytes=LARGE_DLC_BYTES * 14),
]


def resolve(pid: str, include_dlc: bool) -> dict[str, Any]:
    if pid == PID:
        return plan()
    assert include_dlc, "a DLC is always resolved with --include-dlc"
    return {"schema": 1, "rootProductId": pid, "market": "IN", "status": "choice", "choices": [
        {"contentId": CIDS[pid], "kind": "Durable", "packageFormat": "MSIXVC",
         "platform": "Windows.Desktop", "productId": pid, "supported": True,
         "parentProductId": PID, "title": pid},
    ]}


def on_spawn(dest: Path) -> None:
    if dest.parent.name == "dlc":
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "MicrosoftGame.config").write_text("<Game/>")
    else:
        write_game(dest)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("unifideck.utils.paths.get_games_map_path",
                        lambda config=None: str(tmp_path / "games.map"))

    async def fake_terminate(proc: Any, _prefix: str, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(helpers, "terminate_process_tree", fake_terminate)

    @contextlib.contextmanager
    def fake_components(**_kw: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(install_steps, "reading_components", fake_components)
    return {"tmp": tmp_path, "base": tmp_path / "games", "root": tmp_path / "games" / "Xbox" / PID}


def hooks(env: dict[str, Any], owned: list[DlcInfo] | None = OWNED) -> tuple[XboxPcStoreHooks, FakeCli]:
    cli = FakeCli(env["tmp"], resolve=resolve, on_spawn=on_spawn)

    async def dlc_for(_game_id: str) -> list[DlcInfo] | None:
        return owned

    return XboxPcStoreHooks(FakeBus(), FakeXboxPc(), cli, lambda _g: title_info(), dlc_for), cli


def spawned(cli: FakeCli) -> list[str]:
    return [pid for pid, *_ in cli.spawned]


# ── install ─────────────────────────────────────────────────────────────
async def test_install_ends_with_the_game_and_leaves_the_dlc_pending(env: dict[str, Any]) -> None:
    """The install job never streams DLC: cancelling the DLC job cannot undo it."""
    h, cli = hooks(env)
    result = await h.install(PID, str(env["base"]))
    assert result.success, result.error
    assert spawned(cli) == [PID]
    record = read_install_record(PID)
    assert record is not None and record["state"] == "installed"
    entries = dlc_entries(record)
    assert entries[SMALL]["state"] == "selected" and entries[LARGE]["state"] == "declined"
    assert pending_dlc(record) == [SMALL]


async def test_the_follow_up_update_downloads_only_the_dlc(env: dict[str, Any]) -> None:
    h, cli = hooks(env)
    assert (await h.install(PID, str(env["base"]))).success
    assert (await h.update(PID)).success
    assert spawned(cli) == [PID, SMALL], "the game is not streamed again"
    record = read_install_record(PID)
    assert record is not None and pending_dlc(record) == []
    assert (env["root"] / "dlc" / SMALL / "MicrosoftGame.config").is_file()


async def test_install_applies_the_saved_ticks(env: dict[str, Any]) -> None:
    save_choice(PID, [LARGE, SMALL])
    h, cli = hooks(env)
    assert (await h.install(PID, str(env["base"]))).success
    record = read_install_record(PID)
    assert record is not None and pending_dlc(record) == [LARGE, SMALL]
    assert read_choice(PID) is None, "the choice is in the record now"
    assert (await h.update(PID)).success
    assert spawned(cli) == [PID, LARGE, SMALL]


async def test_unreadable_dlc_still_installs_the_game(env: dict[str, Any]) -> None:
    h, cli = hooks(env, owned=None)
    assert (await h.install(PID, str(env["base"]))).success
    assert spawned(cli) == [PID]
    record = read_install_record(PID)
    assert record is not None and "dlc" not in record


async def test_a_failed_dlc_fails_the_update_but_keeps_the_game(env: dict[str, Any]) -> None:
    h, cli = hooks(env)
    assert (await h.install(PID, str(env["base"]))).success
    cli.on_spawn = lambda d: d.mkdir(parents=True, exist_ok=True)  # no package files
    result = await h.update(PID)
    assert not result.success and result.error_code == "xbox_dlc_failed"
    assert "Small pack" in (result.error or "")
    record = read_install_record(PID)
    assert record is not None and record["state"] == "installed" and pending_dlc(record) == [SMALL]


# ── update ──────────────────────────────────────────────────────────────
def installed(env: dict[str, Any], **dlc: str) -> None:
    write_game(env["root"] / "game")
    write_install_record({
        "schema": 1, "productId": PID, "state": "installed", "market": "IN",
        "contentId": plan()["choices"][0]["contentId"], "installRoot": str(env["root"]),
        "gameDir": str(env["root"] / "game"), "exe": str(env["root"] / "game" / "love.exe"),
        "exeRelative": "love.exe", "version": "1.0.5.0",
        "dlc": {pid: {"state": s, "title": pid} for pid, s in dlc.items()},
    })


async def test_update_with_only_dlc_pending_does_not_stream_the_game(env: dict[str, Any]) -> None:
    installed(env, **{LARGE: "selected", SMALL: "installed"})
    h, cli = hooks(env)
    result = await h.update(PID)
    assert result.success and result.metadata["exe_path"].endswith("love.exe")
    assert spawned(cli) == [LARGE]


async def test_update_with_a_game_update_streams_both(env: dict[str, Any]) -> None:
    installed(env, **{LARGE: "failed"})
    h, cli = hooks(env)
    h._updates._newer = [PID]
    assert (await h.update(PID)).success
    assert spawned(cli) == [PID, LARGE]


async def test_dlc_never_downloads_while_the_game_runs(env: dict[str, Any]) -> None:
    installed(env, **{LARGE: "selected"})
    h, cli = hooks(env)
    with game_lock(PID) as held:
        assert held
        result = await h.dlc.run(PID)
    assert not result.success and result.error_code == "xbox_game_running"
    assert cli.spawned == []


def test_initial_dlc_follows_the_saved_ticks_else_small_only() -> None:
    rec = initial_dlc({"schema": 1}, OWNED, None)
    assert {p: e["state"] for p, e in rec["dlc"].items()} == {SMALL: "selected", LARGE: "declined"}
    rec = initial_dlc({"schema": 1}, OWNED, {LARGE})
    assert {p: e["state"] for p, e in rec["dlc"].items()} == {SMALL: "declined", LARGE: "selected"}
    assert rec["dlc"][LARGE]["title"] == "Big expansion"


async def test_dlc_page_ticks_before_install_drive_the_install(env: dict[str, Any]) -> None:
    """Untick the small DLC on the page before install: only the large one downloads."""
    assert (await apply_choice(PID, OWNED, [LARGE]))["installed"] is False
    view = await view_for(PID, OWNED)
    assert {d["product_id"]: d["chosen"] for d in view["dlc"]} == {SMALL: False, LARGE: True}
    h, cli = hooks(env)
    assert (await h.install(PID, str(env["base"]))).success
    record = read_install_record(PID)
    assert record is not None and pending_dlc(record) == [LARGE]


async def test_before_any_choice_every_dlc_shows_ticked(env: dict[str, Any]) -> None:
    view = await view_for(PID, OWNED)
    assert view["installed"] is False and all(d["chosen"] for d in view["dlc"])
