"""Tests for EA installs: records, the installer, Touchup and the prefix seam.

A fake ``maxima-cli`` (a small Python script per test) prints the JSON-lines
the real CLI printed on 2026-11-15 and records how it was called. Every test
points ``HOME`` at a scratch folder: the records and data folders live there.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.launcher.ea import touchup
from unifideck.launcher.ea.records import (
    InstallRecord,
    all_records,
    delete_record,
    read_record,
    write_record,
)
from unifideck.launcher.proton import store_setup
from unifideck.stores.ea.cli import MaximaCli
from unifideck.stores.ea.install import (
    EAInstaller,
    exe_relative_path,
    locate_exe,
    manifest_exe_template,
    resolve_case_insensitive,
)
from unifideck.stores.ea.library import EALibraryReader
from unifideck.utils import config_helpers

_ENTRY: dict[str, Any] = {
    "offer_id": "OFB-EAST:59474",
    "content_id": "1002980",
    "slug": "dragon-age-2",
    "name": "Dragon Age 2",
    "ownership": ["XGP_VAULT"],
    "execute_path": None,
    "install_check": "[HKEY_LOCAL_MACHINE\\SOFTWARE\\BioWare\\Dragon Age II\\Install Dir]bin_ship\\DragonAge2.exe",
}
_GAME_ID = "OFB-EAST__59474"

_FAKE = """#!{python}
import json, os, sys
open({record!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
    "env": {{k: v for k, v in os.environ.items() if k.startswith("MAXIMA_") or k in ("DISPLAY", "XAUTHORITY")}}}}) + "\\n")
args = sys.argv[1:]
if args[0] == "claim":
    sys.stderr.write("ERROR - EA refused the claim: NotFreeOrNoAppropriateSubscriptionError\\n" if {claim_rc} else "")
    print(json.dumps({{"claimed": True, "owned": True, "offer_id": args[2]}}) if not {claim_rc} else "")
    sys.exit({claim_rc})
if args[0] == "build-locales":
    print(json.dumps({{"build_id": "/b.zip", "locales": ["en_US", "de_DE", "pl_PL"], "split": {split}}}))
    sys.exit(0)
if args[0] == "install":
    path = args[args.index("--path") + 1]
    os.makedirs(os.path.join(path, "bin_ship"), exist_ok=True)
    open(os.path.join(path, "bin_ship", "DragonAge2.exe"), "w").write("exe")
print(json.dumps({{"event": "build", "build_id": "/b.zip", "version": "1.04.8524.0"}}))
if "--locale" in args:
    print(json.dumps({{"event": "locale", "locale": args[args.index("--locale") + 1].replace("-", "_")}}))
print(json.dumps({{"event": "progress", "downloaded": 50, "total": 100}}))
sys.stderr.write("INFO - downloading\\n")
print({final!r})
sys.exit({rc})
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _fake_cli(
    home: Path, *, rc: int = 0, final: str = '{"event": "done"}', claim_rc: int = 0, split: bool = True,
) -> tuple[str, Path]:
    calls = home / "calls.jsonl"
    script = home / "maxima-cli"
    script.write_text(_FAKE.format(
        python=sys.executable, record=str(calls), final=final, rc=rc, claim_rc=claim_rc, split=split,
    ))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), calls


def _record(home: Path, **over: str) -> InstallRecord:
    fields = dict(
        game_id=_GAME_ID, offer_id="OFB-EAST:59474", content_id="1002980",
        install_path=str(home / "Games/EA/dragon-age-2"), exe="x", build_id="/b.zip", version="1",
    )
    return InstallRecord(**{**fields, **over})


def test_records_round_trip(home: Path) -> None:
    record = _record(home)
    write_record(record)
    assert read_record(_GAME_ID) == record
    assert all_records() == {_GAME_ID: record}
    delete_record(_GAME_ID)
    delete_record(_GAME_ID)
    assert read_record(_GAME_ID) is None


def test_exe_comes_from_the_offer_template_case_insensitively(tmp_path: Path) -> None:
    assert exe_relative_path(_ENTRY["install_check"]) == "bin_ship/DragonAge2.exe"
    assert exe_relative_path("[HKLM\\X\\Install Dir]\\Game.exe") == "Game.exe"
    assert exe_relative_path("[HKLM\\X\\Install Dir]") is None
    (tmp_path / "Bin_Ship").mkdir()
    (tmp_path / "Bin_Ship" / "dragonage2.EXE").write_text("")
    assert resolve_case_insensitive(tmp_path, "bin_ship/DragonAge2.exe") == tmp_path / "Bin_Ship" / "dragonage2.EXE"
    assert resolve_case_insensitive(tmp_path, "bin_ship/Missing.exe") is None


_MANIFEST = """<?xml version='1.0' encoding='utf-8'?>
<DiPManifest version="4.0">
  <runtime>
    <launcher uid="1-0">
      <filePath>[HKEY_LOCAL_MACHINE\\SOFTWARE\\Studio\\Game\\Install Dir]Trial.exe</filePath>
      <trial>1</trial>
    </launcher>
    <launcher uid="1-1">
      <filePath>[HKEY_LOCAL_MACHINE\\SOFTWARE\\Studio\\Game &amp; Co\\Install Dir]Game.exe</filePath>
      <parameters />
      <trial>0</trial>
      <hidden>0</hidden>
    </launcher>
  </runtime>
</DiPManifest>
"""


def test_exe_falls_back_to_the_build_manifest(tmp_path: Path) -> None:
    entry = {**_ENTRY, "execute_path": None, "install_check": None}
    (tmp_path / "__Installer").mkdir()
    (tmp_path / "__Installer" / "installerdata.xml").write_text(_MANIFEST)
    (tmp_path / "Game.exe").write_text("")
    (tmp_path / "Trial.exe").write_text("")
    assert manifest_exe_template(tmp_path) == "[HKEY_LOCAL_MACHINE\\SOFTWARE\\Studio\\Game & Co\\Install Dir]Game.exe"
    assert locate_exe(tmp_path, entry) == (tmp_path / "Game.exe", "Game.exe")
    # The offer's template still wins when EA gives one.
    (tmp_path / "bin_ship").mkdir()
    (tmp_path / "bin_ship" / "DragonAge2.exe").write_text("")
    assert locate_exe(tmp_path, _ENTRY) == (tmp_path / "bin_ship" / "DragonAge2.exe", "bin_ship/DragonAge2.exe")


def test_no_exe_path_anywhere_names_both_sources(tmp_path: Path) -> None:
    entry = {**_ENTRY, "execute_path": None, "install_check": None}
    assert manifest_exe_template(tmp_path) is None
    (tmp_path / "__Installer").mkdir()
    (tmp_path / "__Installer" / "installerdata.xml").write_text("<DiPManifest><runtime /></DiPManifest>")
    assert manifest_exe_template(tmp_path) is None
    assert locate_exe(tmp_path, entry) == "no game path in the EA offer or the build manifest"


async def test_install_records_the_build_and_reports_progress(home: Path) -> None:
    cli_path, calls = _fake_cli(home)
    progress = AsyncMock()
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=_ENTRY))

    result = await installer.install(_GAME_ID, str(home / "Games"), progress)

    folder = home / "Games" / "EA" / "dragon-age-2"
    assert result.success, result.error
    assert result.install_path == str(folder)
    call = json.loads(calls.read_text().splitlines()[0])
    assert call["argv"] == ["install", "--offer-id", "OFB-EAST:59474", "--path", str(folder)]
    assert call["cwd"].endswith("/ea/maxima/run")
    record = read_record(_GAME_ID)
    assert record is not None
    assert record.exe == str(folder / "bin_ship" / "DragonAge2.exe")
    # What the download worker writes to games.map; without it the worker guessed a trial exe.
    assert result.metadata["exe_path"] == record.exe
    assert (record.build_id, record.version) == ("/b.zip", "1.04.8524.0")
    assert (folder / ".unifideck_manifest.json").is_file()
    first = progress.await_args_list[0].args[0]
    assert first["percentage"] == 50.0 and first["total_bytes"] == 100


async def test_languages_claim_an_ea_play_game_then_read_the_build(home: Path) -> None:
    cli_path, calls = _fake_cli(home)
    entry = {**_ENTRY, "ownership": [], "claimable": True}
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=entry))

    assert await installer.languages(_GAME_ID) == ["en-US", "de-DE", "pl-PL"]

    argvs = [json.loads(line)["argv"] for line in calls.read_text().splitlines()]
    assert argvs == [["claim", "--offer-id", "OFB-EAST:59474"], ["build-locales", "--offer-id", "OFB-EAST:59474"]]
    assert entry["claimable"] is False


async def test_languages_sharing_the_same_game_files_need_no_picker(home: Path) -> None:
    cli_path, _ = _fake_cli(home, split=False)
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=_ENTRY))
    assert await installer.languages(_GAME_ID) == []


async def test_languages_of_a_refused_claim_are_empty(home: Path) -> None:
    cli_path, calls = _fake_cli(home, claim_rc=1)
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value={**_ENTRY, "claimable": True}))
    assert await installer.languages(_GAME_ID) == []
    assert [json.loads(line)["argv"][0] for line in calls.read_text().splitlines()] == ["claim"]


async def test_the_picked_language_is_installed_recorded_and_kept_on_update(home: Path) -> None:
    cli_path, calls = _fake_cli(home)
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=_ENTRY))

    result = await installer.install(_GAME_ID, str(home / "Games"), None, "de-DE")

    assert result.success, result.error
    record = read_record(_GAME_ID)
    assert record is not None and record.locale == "de_DE"
    assert (await installer.update(_GAME_ID, None)).success
    argvs = [json.loads(line)["argv"] for line in calls.read_text().splitlines()]
    assert argvs[0][-2:] == ["--locale", "de-DE"]
    assert argvs[1][-2:] == ["--locale", "de_DE"]


async def test_no_language_leaves_the_build_default(home: Path) -> None:
    cli_path, calls = _fake_cli(home)
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=_ENTRY))
    assert (await installer.install(_GAME_ID, str(home / "Games"), None)).success
    assert "--locale" not in json.loads(calls.read_text().splitlines()[0])["argv"]
    record = read_record(_GAME_ID)
    assert record is not None and record.locale == ""


async def test_a_failed_install_keeps_no_record(home: Path) -> None:
    cli_path, _ = _fake_cli(home, rc=1, final="ERROR - Native(Wine(Fetch))")
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=_ENTRY))
    result = await installer.install(_GAME_ID, str(home / "Games"), None)
    assert not result.success
    assert result.error is not None and result.error.startswith("ea_install_failed")
    assert read_record(_GAME_ID) is None


async def test_an_ea_play_game_is_claimed_before_its_first_download(home: Path) -> None:
    cli_path, calls = _fake_cli(home)
    entry = {**_ENTRY, "ownership": [], "claimable": True}
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=entry))

    result = await installer.install(_GAME_ID, str(home / "Games"), None)

    assert result.success, result.error
    argvs = [json.loads(line)["argv"] for line in calls.read_text().splitlines()]
    assert argvs[0] == ["claim", "--offer-id", "OFB-EAST:59474"]
    assert argvs[1][0] == "install"
    # Owned from here on: a retry or an update must not claim again.
    assert entry["claimable"] is False


async def test_an_unclaimed_game_has_no_size_and_asks_ea_nothing(home: Path) -> None:
    cli_path, calls = _fake_cli(home)
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value={**_ENTRY, "claimable": True}))
    assert await installer.size(_GAME_ID) is None
    assert not calls.exists()


async def test_an_owned_game_is_never_claimed(home: Path) -> None:
    cli_path, calls = _fake_cli(home)
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=_ENTRY))
    assert (await installer.install(_GAME_ID, str(home / "Games"), None)).success
    assert [json.loads(line)["argv"][0] for line in calls.read_text().splitlines()] == ["install"]


async def test_a_refused_claim_fails_the_install_without_downloading(home: Path) -> None:
    cli_path, calls = _fake_cli(home, claim_rc=1)
    entry = {**_ENTRY, "ownership": [], "claimable": True}
    installer = EAInstaller(MaximaCli(cli_path), AsyncMock(return_value=entry))

    result = await installer.install(_GAME_ID, str(home / "Games"), None)

    assert not result.success
    assert result.error is not None and result.error.startswith("ea_claim_failed: ")
    assert "NotFreeOrNoAppropriateSubscriptionError" in result.error
    assert [json.loads(line)["argv"][0] for line in calls.read_text().splitlines()] == ["claim"]
    assert entry["claimable"] is True
    assert read_record(_GAME_ID) is None


async def test_uninstall_removes_folder_and_record(home: Path) -> None:
    folder = home / "Games/EA/dragon-age-2"
    folder.mkdir(parents=True)
    write_record(_record(home))
    installer = EAInstaller(MaximaCli(None), AsyncMock(return_value=_ENTRY))
    assert (await installer.uninstall(_GAME_ID)).success
    assert not folder.exists() and read_record(_GAME_ID) is None


async def test_library_shows_recorded_installs(home: Path) -> None:
    folder = home / "Games/EA/dragon-age-2"
    (folder / "bin_ship").mkdir(parents=True)
    exe = folder / "bin_ship" / "DragonAge2.exe"
    exe.write_text("")
    write_record(_record(home, exe=str(exe)))
    cli = MagicMock()
    cli.list_games = AsyncMock(return_value=[_ENTRY])
    reader = EALibraryReader(cli)
    games = await reader.get_library()
    assert games is not None and games[0].installed
    assert games[0].exe_path == str(exe)
    assert await reader.entry(_GAME_ID) == _ENTRY


async def test_touchup_runs_once_per_build_with_the_game_prefix(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_path, calls = _fake_cli(home, final='{"event": "done", "version": "1"}')
    monkeypatch.setattr(config_helpers, "_read_cold_start_json", lambda: {"stores": {"ea": {"cli_path": cli_path}}})
    record = _record(home)
    prefix = home / "pfx"
    prefix.mkdir()
    proton = home / "GE" / "proton"
    umu = home / "umu-run"

    assert touchup.touchup_needed(prefix, record)
    assert await touchup.run_touchup(record, prefix, proton, umu, None)
    assert not touchup.touchup_needed(prefix, record)
    assert touchup.touchup_needed(prefix, _record(home, build_id="/new.zip"))

    call = json.loads(calls.read_text().splitlines()[0])
    assert call["argv"] == ["touchup", "--path", record.install_path]
    # The recorded install language reaches Touchup.
    german = _record(home, build_id="/de.zip", locale="de_DE")
    assert await touchup.run_touchup(german, prefix, proton, umu, None)
    assert json.loads(calls.read_text().splitlines()[-1])["argv"][-2:] == ["--locale", "de_DE"]
    assert call["env"]["MAXIMA_WINE_PREFIX"] == str(prefix)
    assert call["env"]["MAXIMA_PROTON"] == str(home / "GE")
    assert call["env"]["MAXIMA_UMU"] == str(umu)


async def test_prefix_step_table() -> None:
    ctx = MagicMock(store="epic")
    assert await store_setup.run_store_prefix_setup(ctx, MagicMock())
    assert store_setup.STORE_PREFIX_SETUP["ea"] is touchup.prepare_prefix


async def test_prepare_prefix_without_a_record_fails_loudly(home: Path) -> None:
    ctx = MagicMock(game_id=_GAME_ID, plugin_dir=None)
    assert not await touchup.prepare_prefix(ctx, MagicMock())


def test_cold_start_string_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_helpers, "_read_cold_start_json", lambda: {"a": {"b": "x", "n": 3, "e": ""}})
    assert config_helpers.read_config_str_cold_start("a.b") == "x"
    assert config_helpers.read_config_str_cold_start("a.n", "d") == "d"
    assert config_helpers.read_config_str_cold_start("a.e", "d") == "d"
    assert config_helpers.read_config_str_cold_start("a.missing", "d") == "d"
    assert config_helpers.read_config_int_cold_start("a.n", 1) == 3


async def test_touchup_borrows_the_session_display_without_overriding(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The headless plugin has no display; without one Wine's desktop never starts."""
    cli_path, calls = _fake_cli(home, final='{"event": "done"}')
    monkeypatch.setattr(config_helpers, "_read_cold_start_json", lambda: {"stores": {"ea": {"cli_path": cli_path}}})
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("XAUTHORITY", "/own/xauth")
    prefix = home / "pfx"
    prefix.mkdir()
    session = {"DISPLAY": ":0", "XAUTHORITY": "/borrowed/xauth"}
    assert await touchup.run_touchup(_record(home), prefix, home / "GE" / "proton", home / "umu-run", None, session)
    env = json.loads(calls.read_text().splitlines()[0])["env"]
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/own/xauth"


async def test_the_store_names_the_recorded_exe_never_a_guess(home: Path) -> None:
    """A build shipping a trial exe next to the game must launch the game (Burnout, 2026-11-15)."""
    from unifideck.stores.ea.store import EAStore

    folder = home / "Games" / "EA" / "game"
    folder.mkdir(parents=True)
    (folder / "Game.exe").write_bytes(b"MZ")
    (folder / "Game_trial.exe").write_bytes(b"MZ" * 100)
    write_record(InstallRecord(
        game_id=_GAME_ID, offer_id="OFB-EAST:59474", content_id="1", install_path=str(folder),
        exe=str(folder / "Game.exe"), build_id="/b.zip", version="1",
    ))
    store = object.__new__(EAStore)

    assert await store.find_installed_exe(str(folder), _GAME_ID) == str(folder / "Game.exe")
    assert await store.find_installed_exe(str(folder), "Origin.OFR.50.0000001") is None
