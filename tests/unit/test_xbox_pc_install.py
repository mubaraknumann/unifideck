"""Xbox PC install: preconditions, the install root, records, repair and cancel.

The install root ``<base>/Xbox/<PID>`` is the unit of install, marker and
deletion; the game files are in its ``game/`` folder. Every path here is
under ``tmp_path`` (``XDG_DATA_HOME`` and ``games.map`` are redirected).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _xbox_pc_fakes import (
    CID,
    PID,
    FakeCli,
    FakeCliError,
    FakeProc,
    FakeStdout,
    FakeXboxPc,
    plan,
    record_line,
    title_info,
)

from unifideck.launcher.xbox_pc import component_locks
from unifideck.launcher.xbox_pc.install_records import read_install_record, write_install_record
from unifideck.services.download.models import DownloadItem
from unifideck.services.download.worker_helpers import prefix_warmup_supported
from unifideck.stores.microsoft.xodus import install_steps
from unifideck.stores.microsoft.xodus import installer as installer_mod
from unifideck.stores.microsoft.xodus.installer import XboxPcInstaller
from unifideck.stores.shared import cli_install_helpers as helpers


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data = tmp_path / "data"
    games_map = tmp_path / "games.map"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr("unifideck.utils.paths.get_games_map_path",
                        lambda config=None: str(games_map))
    kills: list[int] = []

    async def fake_terminate(proc: Any, _prefix: str, **_kw: Any) -> None:
        kills.append(proc.pid)
        proc.returncode = -9

    monkeypatch.setattr(helpers, "terminate_process_tree", fake_terminate)
    held: list[str] = []

    @contextlib.contextmanager
    def fake_components(**_kw: Any) -> Iterator[object]:
        held.append("in")
        try:
            yield object()
        finally:
            held.append("out")

    monkeypatch.setattr(install_steps, "reading_components", fake_components)
    base = tmp_path / "games"
    base.mkdir()
    return {"tmp": tmp_path, "base": base, "games_map": games_map, "kills": kills,  # type: ignore[dict-item]
            "held": held}


def _installer(env: dict[str, Any], *, xbox: FakeXboxPc | None = None,
               cli: FakeCli | None = None, info: Any = "default") -> tuple[XboxPcInstaller, FakeCli]:
    cli = cli or FakeCli(env["tmp"])
    meta = title_info() if info == "default" else info
    return XboxPcInstaller(xbox or FakeXboxPc(), cli, lambda pid: meta), cli


def _root(env: dict[str, Any]) -> Path:
    return env["base"] / "Xbox" / PID


# ── precondition ────────────────────────────────────────────────────────
@pytest.mark.parametrize(("xbox", "info", "code"), [
    (FakeXboxPc(enabled=False), title_info(), "xbox_pc_disabled"),
    (FakeXboxPc(runtime="missing"), title_info(), "xbox_pc_runtime_missing"),
    (FakeXboxPc(account="signed_out"), title_info(), "xbox_pc_not_signed_in"),
    (FakeXboxPc(), title_info(state="not_supported", reason="ea_play"), "xbox_pc_unsupported_plan"),
    (FakeXboxPc(), title_info(state="choice", content_id=None), "xbox_pc_needs_choice"),
    (FakeXboxPc(), title_info(state="choice", reason="needs_choice"), "xbox_pc_needs_choice"),
    (FakeXboxPc(), title_info(content_id=None), "xbox_pc_no_package"),
    (FakeXboxPc(), None, "not_supported"),
])
async def test_precondition_codes(env: dict[str, Any], xbox: FakeXboxPc, info: Any,
                                  code: str) -> None:
    inst, _ = _installer(env, xbox=xbox, info=info)
    assert await inst.precondition(PID) == code


async def test_precondition_passes_for_a_ready_title(env: dict[str, Any]) -> None:
    inst, _ = _installer(env)
    assert await inst.precondition(PID) is None
    # A choice row whose one Game the library already picked installs too.
    picked, _ = _installer(env, info=title_info(state="choice"))
    assert await picked.precondition(PID) is None
    # A row id that is not a product id needs the library's product id.
    no_pid, _ = _installer(env, info=title_info(product_id=None))
    assert await no_pid.precondition("xcloud-only") == "not_supported"


async def test_unverified_gamertag_still_counts_as_signed_in(env: dict[str, Any]) -> None:
    inst, _ = _installer(env, xbox=FakeXboxPc(account="unverified"))
    assert await inst.precondition(PID) is None


# ── a clean install ─────────────────────────────────────────────────────
async def test_install_lands_in_the_install_root(env: dict[str, Any]) -> None:
    inst, cli = _installer(env)
    progress: list[dict[str, Any]] = []

    async def cb(update: dict[str, Any]) -> None:
        progress.append(update)

    result = await inst.install(PID, str(env["base"]), cb)
    root = _root(env)
    assert result.success, result.error
    assert result.install_path == str(root)
    exe = root.resolve() / "game" / "love.exe"
    assert result.metadata == {"exe_path": str(exe), "runtime_managed_prefix": True}
    assert cli.spawned == [(PID, str(root / "game"), "IN", CID)]
    record = read_install_record(PID)
    assert record is not None and record["state"] == "installed"
    assert record["installRoot"] == str(root) and record["gameDir"] == str(root / "game")
    assert record["exeRelative"] == "love.exe" and record["version"] == "1.0.5.0"
    assert record["titleId"] == "6adc6be5" and record["contentId"] == CID
    marker = json.loads((root / ".unifideck_manifest.json").read_text())
    assert marker["store"] == "microsoft" and marker["store_id"] == PID
    assert progress and progress[-1]["percentage"] == 100.0


async def test_a_row_id_other_than_the_product_id(env: dict[str, Any]) -> None:
    # A merged row keeps its own store_game_id; Xodus gets the product id.
    inst, cli = _installer(env, info=title_info(product_id=PID, title="Balatro"))
    result = await inst.install("xcloud-balatro", str(env["base"]))
    root = _root(env)
    assert result.success and result.game_id == "xcloud-balatro"
    assert cli.spawned[0][0] == PID
    record = read_install_record(PID)
    assert record is not None and record["storeGameId"] == "xcloud-balatro"
    assert record["title"] == "Balatro"
    marker = json.loads((root / ".unifideck_manifest.json").read_text())
    assert marker["store_id"] == "xcloud-balatro"


async def test_the_marker_is_found_by_the_marker_sweep(env: dict[str, Any]) -> None:
    from unifideck.core import marker_sweep

    inst, _ = _installer(env)
    await inst.install(PID, str(env["base"]))
    root = _root(env)
    assert marker_sweep.find_for_game({root.parent}, "microsoft", PID) == root


def test_the_queue_skips_the_ge_prefix_warmup(env: dict[str, Any]) -> None:
    item = DownloadItem(store="microsoft", game_id=PID, title="t", install_path=str(env["base"]))
    result = SimpleNamespace(metadata={"exe_path": "/x/love.exe", "runtime_managed_prefix": True})
    assert prefix_warmup_supported(item, result) is False
    plain = SimpleNamespace(metadata={"exe_path": "/x/love.exe"})
    assert prefix_warmup_supported(item, plain) is True


async def test_the_market_falls_back_to_the_session(env: dict[str, Any]) -> None:
    inst, cli = _installer(env, xbox=FakeXboxPc(market="GB"), info=title_info(market=None))
    assert (await inst.install(PID, str(env["base"]))).success
    assert cli.spawned[0][2] == "GB"


async def test_no_market_at_all_is_refused(env: dict[str, Any]) -> None:
    inst, cli = _installer(env, xbox=FakeXboxPc(market=None), info=title_info(market="??"))
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_market_unknown" and cli.spawned == []


# ── failures ────────────────────────────────────────────────────────────
async def test_a_missing_backend_fails_before_any_folder_exists(env: dict[str, Any]) -> None:
    inst, cli = _installer(env)
    cli.backend_error = FakeCliError("backend_tampered")
    result = await inst.install(PID, str(env["base"]))
    assert result.error == "xodus_backend_tampered"
    assert not _root(env).exists()


async def test_a_resolve_error_is_a_resolve_failure(env: dict[str, Any]) -> None:
    cli = FakeCli(env["tmp"], resolve=FakeCliError("timeout"))
    inst, _ = _installer(env, cli=cli)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_resolve_failed"


async def test_two_editions_are_refused(env: dict[str, Any]) -> None:
    from _xbox_pc_fakes import game_choice

    other = "11111111-2222-3333-4444-555555555555"
    cli = FakeCli(env["tmp"], resolve=plan(game_choice(CID), game_choice(other), status="choice"))
    inst, _ = _installer(env, cli=cli)
    assert (await inst.install(PID, str(env["base"]))).error_code == "xbox_package_choice"


async def test_a_folder_holding_another_store_is_refused(env: dict[str, Any]) -> None:
    root = _root(env)
    env["games_map"].write_text(f"gog:1=\t{root}/game\t1\n")
    inst, cli = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_folder_in_use" and cli.spawned == []


async def test_a_filesystem_without_symlinks_is_refused(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer_mod, "symlink_supported", lambda _p: False)
    inst, cli = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_filesystem_unsupported" and cli.spawned == []


@pytest.mark.parametrize(("kw", "missing"), [
    ({"container": False}, "container_missing"),
    ({"exe": False}, "exe_missing"),
])
async def test_a_complete_run_with_files_missing_is_incomplete(
    env: dict[str, Any], kw: dict[str, bool], missing: str,
) -> None:
    from _xbox_pc_fakes import write_game

    cli = FakeCli(env["tmp"], on_spawn=lambda dest: write_game(dest, **kw))
    inst, _ = _installer(env, cli=cli)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_install_incomplete" and missing in str(result.error)
    record = read_install_record(PID)
    assert record is not None and record["state"] == "partial"


async def test_a_lowercase_manifest_name_is_found(env: dict[str, Any]) -> None:
    from _xbox_pc_fakes import write_game

    cli = FakeCli(env["tmp"], on_spawn=lambda d: write_game(d, config_name="microsoftgame.config"))
    inst, _ = _installer(env, cli=cli)
    assert (await inst.install(PID, str(env["base"]))).success


async def test_cancel_kills_the_download_and_keeps_the_partial(env: dict[str, Any]) -> None:
    hang = FakeStdout([record_line(0, "metadata")], hang_at_end=True)
    procs: list[FakeProc] = []

    def make() -> FakeProc:
        procs.append(FakeProc(hang))
        return procs[-1]

    cli = FakeCli(env["tmp"], make_proc=make, on_spawn=lambda d: d.mkdir(parents=True, exist_ok=True))
    inst, _ = _installer(env, cli=cli)
    task = asyncio.ensure_future(inst.install(PID, str(env["base"])))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env["kills"] == [procs[0].pid]
    record = read_install_record(PID)
    assert record is not None and record["state"] == "partial" and record["contentId"] == CID
    marker = json.loads((_root(env) / ".unifideck_manifest.json").read_text())
    assert marker["store"] == "microsoft", "a partial download is marked as ours"
    assert not cli.credential_lock.locked()
    assert env["held"] == ["in", "out"]


async def test_no_installed_components_is_runtime_missing(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_steps, "reading_components", component_locks.reading_components)
    inst, cli = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_pc_runtime_missing" and cli.spawned == []


async def test_a_component_promote_in_progress_is_components_busy(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.contextmanager
    def busy(**_kw: Any) -> Iterator[object]:
        raise component_locks.ComponentsBusyError("install.lock held")
        yield  # pragma: no cover

    monkeypatch.setattr(install_steps, "reading_components", busy)
    inst, _ = _installer(env)
    assert (await inst.install(PID, str(env["base"]))).error_code == "xodus_components_busy"


# ── an earlier record of the same game ──────────────────────────────────
def _record(root: Path, state: str, cid: str = CID) -> dict[str, Any]:
    return {"schema": 1, "productId": PID, "state": state, "market": "IN", "contentId": cid,
            "installRoot": str(root), "gameDir": str(root / "game"), "startedAt": 1.0}


async def test_same_root_installed_is_a_repair(env: dict[str, Any]) -> None:
    write_install_record(_record(_root(env), "installed"))
    inst, cli = _installer(env)
    assert (await inst.install(PID, str(env["base"]))).success
    assert len(cli.spawned) == 1


async def test_installed_on_another_drive_is_refused(env: dict[str, Any]) -> None:
    write_install_record(_record(env["tmp"] / "sd" / "Xbox" / PID, "installed"))
    inst, cli = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_already_installed" and cli.spawned == []


async def test_an_unfinished_update_must_finish_first(env: dict[str, Any]) -> None:
    write_install_record(_record(_root(env), "updating"))
    inst, _ = _installer(env)
    assert (await inst.install(PID, str(env["base"]))).error_code == "xbox_update_pending"


async def test_same_root_partial_resumes_with_its_content_id(env: dict[str, Any]) -> None:
    write_install_record(_record(_root(env), "partial"))
    inst, cli = _installer(env)
    assert (await inst.install(PID, str(env["base"]))).success
    assert cli.spawned[0][3] == CID


async def test_an_old_partial_elsewhere_is_deleted_when_the_guards_allow(
    env: dict[str, Any],
) -> None:
    old = env["tmp"] / "sd" / "Xbox" / PID
    (old / "game").mkdir(parents=True)
    write_install_record(_record(old, "partial"))
    inst, _ = _installer(env)
    assert (await inst.install(PID, str(env["base"]))).success
    assert not old.exists()


async def test_an_old_partial_holding_another_game_is_kept_and_refused(env: dict[str, Any]) -> None:
    """Overwriting the record would leave the old folder with nothing pointing at it."""
    old = env["tmp"] / "sd" / "Xbox" / PID
    (old / "game").mkdir(parents=True)
    env["games_map"].write_text(f"epic:abc=\t{old}/game\t1\n")
    write_install_record(_record(old, "partial"))
    inst, cli = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_old_partial_in_place" and str(old) in str(result.error)
    assert old.exists() and cli.spawned == []
    record = read_install_record(PID)
    assert record is not None and record["installRoot"] == str(old)


async def test_an_old_partial_on_a_drive_that_is_not_there_is_refused(env: dict[str, Any]) -> None:
    old = env["tmp"] / "unmounted-sd" / "Xbox" / PID
    write_install_record(_record(old, "partial"))
    inst, cli = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_install_unreachable" and cli.spawned == []
    record = read_install_record(PID)
    assert record is not None and record["installRoot"] == str(old)


async def test_an_old_partial_deleted_by_hand_starts_fresh(env: dict[str, Any]) -> None:
    old = env["tmp"] / "sd" / "Xbox" / PID
    old.parent.parent.mkdir(parents=True)
    write_install_record(_record(old, "partial"))
    inst, _ = _installer(env)
    assert (await inst.install(PID, str(env["base"]))).success
    record = read_install_record(PID)
    assert record is not None and record["installRoot"] == str(_root(env))


# ── repair, readiness and unexpected failures ────────────────────────────
async def test_a_repair_refuses_while_the_game_runs(env: dict[str, Any]) -> None:
    write_install_record(_record(_root(env), "installed"))
    inst, cli = _installer(env)
    with component_locks.game_lock(PID) as held:
        assert held
        result = await inst.install(PID, str(env["base"]))
    assert result.error_code == "xbox_game_running" and cli.spawned == []


async def test_a_repair_holds_the_game_lock_while_it_streams(env: dict[str, Any]) -> None:
    from _xbox_pc_fakes import write_game

    seen: list[bool] = []

    def on_spawn(dest: Path) -> None:
        with component_locks.game_lock(PID) as free:
            seen.append(free)
        write_game(dest)

    write_install_record(_record(_root(env), "installed"))
    inst, _ = _installer(env, cli=FakeCli(env["tmp"], on_spawn=on_spawn))
    assert (await inst.install(PID, str(env["base"]))).success
    assert seen == [False], "a launch cannot start mid-repair"
    with component_locks.game_lock(PID) as free:
        assert free, "released after the repair"


@pytest.mark.parametrize(("found", "code"), [("signed_in", None), ("signed_out", "xbox_pc_not_signed_in")])
async def test_an_unknown_account_is_asked_about_once(
    env: dict[str, Any], found: str, code: str | None,
) -> None:
    """After a plugin restart the cached account is unknown until someone asks."""
    xbox = FakeXboxPc(account="unknown", checked_account=found)
    inst, _ = _installer(env, xbox=xbox)
    assert await inst.precondition(PID) == code
    assert xbox.checks == 1


async def test_an_unsafe_data_folder_is_a_coded_failure(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextlib.contextmanager
    def unsafe(**_kw: Any) -> Iterator[object]:
        raise PermissionError("folder belongs to uid 0: /home/deck/.local/share/unifideck")
        yield  # pragma: no cover

    monkeypatch.setattr(install_steps, "reading_components", unsafe)
    inst, _ = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error == "xodus_components_unreadable: PermissionError"


async def test_an_unexpected_os_error_is_coded_without_its_path(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_root: Path, _game_id: str) -> None:
        raise PermissionError("/home/deck/private")

    monkeypatch.setattr(installer_mod, "prepare_root", broken)
    inst, _ = _installer(env)
    result = await inst.install(PID, str(env["base"]))
    assert result.error == "xodus_failed: PermissionError" and "/home" not in str(result.error)
