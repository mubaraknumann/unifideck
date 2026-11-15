"""Tests for the EA launch: the ``launch-prepare`` sidecar and the umu handler."""
from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.launcher.ea import sidecar as sidecar_mod
from unifideck.launcher.ea.records import InstallRecord, write_record
from unifideck.launcher.proton import _STORE_LAUNCHERS
from unifideck.launcher.proton.handlers import ea as handler
from unifideck.launcher.types.errors import GameFailedError, GameNotFoundError

# Prints the ready line, then stays until stdin closes, like the real CLI.
_FAKE_READY = """#!{python}
import json, sys
sys.stderr.write("INFO - licence requested\\n")
print(json.dumps({{"event": "ready", "env": {{"EALsxPort": "3216", "MXLaunchId": "abc"}}}}), flush=True)
sys.stdin.read()
print(json.dumps({{"event": "stopped"}}), flush=True)
"""
_FAKE_FAILS = """#!{python}
import sys
sys.stderr.write("ERROR - Not signed in\\n")
sys.exit(1)
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _script(home: Path, body: str) -> Path:
    script = home / "maxima-cli"
    script.write_text(body.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_ea_has_its_own_launcher() -> None:
    assert _STORE_LAUNCHERS["ea"] is handler.ea_launch


async def test_sidecar_hands_over_its_env_and_stops_on_stdin_close(home: Path) -> None:
    cli = _script(home, _FAKE_READY)
    running = await sidecar_mod.start_sidecar(cli, "OFB-EAST:59474", home / "Game.exe", {"PATH": "/usr/bin:/bin"})
    assert running.env == {"EALsxPort": "3216", "MXLaunchId": "abc"}
    await running.stop()
    assert running._proc.returncode == 0
    assert "licence requested" in (home / ".local/share/unifideck/ea/maxima/launch-prepare-OFB-EAST_59474.log").read_text()


async def test_sidecar_that_exits_early_reports_its_log(home: Path) -> None:
    cli = _script(home, _FAKE_FAILS)
    with pytest.raises(sidecar_mod.SidecarError) as err:
        await sidecar_mod.start_sidecar(cli, "OFB-EAST:59474", home / "Game.exe", {"PATH": "/usr/bin:/bin"})
    assert err.value.returncode == 1
    assert "Not signed in" in err.value.detail
    assert err.value.licence_refusal is None


_RAW_REFUSAL = (
    'ERROR - license request failed: `<?xml version="1.0"?><error code="CG_LIMIT_EXCEEDED">'
    '<eadmToken value="SECRET-TOKEN"/></error>`'
)


@pytest.mark.parametrize("line", [_RAW_REFUSAL, "ERROR - license request refused: CG_LIMIT_EXCEEDED"])
async def test_a_refused_licence_names_its_code_and_never_the_token(home: Path, line: str) -> None:
    cli = _script(home, _FAKE_FAILS.replace("ERROR - Not signed in", line))
    with pytest.raises(sidecar_mod.SidecarError) as err:
        await sidecar_mod.start_sidecar(cli, "OFB-EAST:59474", home / "Game.exe", {"PATH": "/usr/bin:/bin"})
    assert err.value.licence_refusal == "CG_LIMIT_EXCEEDED"
    assert "SECRET-TOKEN" not in str(err.value)


def _plan(home: Path) -> MagicMock:
    plan = MagicMock()
    plan.context.game_id = "OFB-EAST__59474"
    plan.context.plugin_dir = None
    plan.context.exe_path = home / "Game.exe"
    plan.state.prefix_path = home / "pfx"
    plan.state.proton_path = home / "GE" / "proton"
    plan.state.umu_wrapper = home / "umu-run"
    plan.state.game_args = ["-windowed"]
    plan.env = {"WINEPREFIX": str(home / "pfx")}
    return plan


def _install(home: Path) -> None:
    write_record(InstallRecord(
        game_id="OFB-EAST__59474", offer_id="OFB-EAST:59474", content_id="1", install_path=str(home),
        exe=str(home / "Game.exe"), build_id="/b.zip", version="1",
    ))


async def test_the_game_runs_with_the_sidecar_env_and_the_sidecar_always_stops(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(home)
    running = MagicMock(env={"EALsxPort": "3216"}, stop=AsyncMock(), origin_standin=None)
    seen: dict[str, Any] = {}

    async def fake_umu(argv: list[str], **kw: Any) -> int:
        seen.update(argv=argv, env=kw["env"])
        return 1

    monkeypatch.setattr(handler.maxima, "find_cli", lambda *_: home / "maxima-cli")
    monkeypatch.setattr(handler, "prepare_prefix", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "start_sidecar", AsyncMock(return_value=running))
    monkeypatch.setattr(handler, "run_umu_with_retry", fake_umu)

    with pytest.raises(GameFailedError):
        await handler.ea_launch(_plan(home))

    assert seen["argv"][2:] == [str(home / "Game.exe"), "-windowed"]
    assert seen["env"]["EALsxPort"] == "3216" and seen["env"]["WINEPREFIX"] == str(home / "pfx")
    sidecar_env = handler.start_sidecar.await_args.args[3]
    assert sidecar_env["MAXIMA_WINE_PREFIX"] == str(home / "pfx")
    running.stop.assert_awaited_once()


async def test_launch_without_an_install_record_is_refused(home: Path) -> None:
    with pytest.raises(GameNotFoundError):
        await handler.ea_launch(_plan(home))


_VC90 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
    <noInheritable></noInheritable>
    <assemblyIdentity type="win32" name="Microsoft.VC90.CRT" version="9.0.21022.8" processorArchitecture="x86" publicKeyToken="1fc8b3b9a1e18e3b"></assemblyIdentity>
    <file name="msvcr90.dll"></file>
</assembly>
"""


def test_bundled_vc90_without_an_exe_manifest_gets_one(tmp_path: Path) -> None:
    from unifideck.launcher.proton.compat.app_local_crt import ensure_crt_activation_manifest

    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ\x00no manifest here")
    (tmp_path / "Microsoft.VC90.CRT.manifest").write_text(_VC90)

    assert ensure_crt_activation_manifest(exe)
    written = (tmp_path / "Game.exe.manifest").read_text()
    assert 'name="Microsoft.VC90.CRT" version="9.0.21022.8"' in written
    assert 'publicKeyToken="1fc8b3b9a1e18e3b"' in written
    assert not ensure_crt_activation_manifest(exe)  # already there: untouched


def test_working_games_are_left_alone(tmp_path: Path) -> None:
    from unifideck.launcher.proton.compat.app_local_crt import ensure_crt_activation_manifest

    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ\x00no manifest here")
    assert not ensure_crt_activation_manifest(exe)  # no bundled runtime
    (tmp_path / "Microsoft.VC90.CRT.manifest").write_text(_VC90)
    exe.write_bytes(b"MZ\x00<assembly xmlns='urn:schemas-microsoft-com:asm.v1'>")
    assert not ensure_crt_activation_manifest(exe)  # the exe embeds its own manifest
    assert not (tmp_path / "Game.exe.manifest").exists()


_ORIGIN = "C:/Program Files (x86)/Origin/Origin.exe"


def _umu_like(tmp_path: Path, *, waits_for_child: bool) -> tuple[Path, Path, Path]:
    """A fake umu run: starts ``Origin.exe`` (own session, as Wine does), notes a SIGTERM.

    ``waits_for_child``: returns when the Wine process ends, like ``runinprefix``.
    """
    pid_file, term_file = tmp_path / "origin.pid", tmp_path / "umu-got-sigterm"
    script = tmp_path / "umu.sh"
    tail = "wait $child\n" if waits_for_child else "while true; do sleep 1; done\n"
    script.write_text(
        "#!/bin/bash\n"
        f"trap 'touch {term_file}; exit 143' TERM\n"
        f"setsid bash -c 'exec -a \"{_ORIGIN}\" sleep 60' & child=$!\n"
        f"echo $child > {pid_file}\n"
        + tail
    )
    script.chmod(0o755)
    return script, pid_file, term_file


async def _run_until_started(standin: Any, script: Path, tmp_path: Path, game_done: Any) -> Any:
    import asyncio
    import os

    env = {**os.environ, "WINEPREFIX": str(tmp_path)}
    task = asyncio.create_task(standin.run_standin([str(script)], env, "Game.exe", tmp_path, game_done))
    for _ in range(200):
        if standin.prefix_pids("Origin.exe", tmp_path):
            break
        await asyncio.sleep(0.05)
    return task


def _gone(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_standin_runs_while_the_game_plays_and_stops_without_a_session_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping it must never SIGTERM umu: umu then tears down the game's Wine session (exit 143)."""
    import asyncio

    from unifideck.launcher.ea import standin

    monkeypatch.setattr(standin, "START_DELAY_S", 0)
    monkeypatch.setattr(standin, "POLL_S", 0.05)
    script, pid_file, term_file = _umu_like(tmp_path, waits_for_child=True)
    game_done = asyncio.Event()

    task = await _run_until_started(standin, script, tmp_path, game_done)
    pid = int(pid_file.read_text())
    assert not _gone(pid)  # running while the game plays
    game_done.set()
    await asyncio.wait_for(task, 15)

    for _ in range(50):
        if _gone(pid):
            break
        await asyncio.sleep(0.05)
    assert _gone(pid)
    assert not term_file.exists(), "the stand-in's umu was sent SIGTERM"


async def test_a_umu_run_that_never_returns_is_ended_as_a_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from unifideck.launcher.ea import standin

    monkeypatch.setattr(standin, "START_DELAY_S", 0)
    monkeypatch.setattr(standin, "POLL_S", 0.05)
    monkeypatch.setattr(standin, "STOP_WAIT_S", 0.5)
    script, pid_file, term_file = _umu_like(tmp_path, waits_for_child=False)
    game_done = asyncio.Event()

    task = await _run_until_started(standin, script, tmp_path, game_done)
    pid = int(pid_file.read_text())
    game_done.set()
    await asyncio.wait_for(task, 15)

    for _ in range(50):
        if _gone(pid) and term_file.exists():
            break
        await asyncio.sleep(0.05)
    assert _gone(pid), "the stand-in's own process outlived the game"
    assert term_file.exists()


def test_only_exes_built_with_the_origin_sdk_get_the_standin(home: Path) -> None:
    """Most Wanted waits for an Origin process; Crysis quits while one runs (2026-11-15)."""
    import json

    from unifideck.launcher.ea import standin
    from unifideck.launcher.ea.records import read_record, records_dir

    sdk_game, plain_game = home / "NFS13.exe", home / "Crysis64.exe"
    sdk_game.write_bytes(b"MZ" + b"\0" * 4096 + b"OriginSDK/9.10" + b"\0" * 64)
    plain_game.write_bytes(b"MZ" + b"\0" * 8192)
    for gid, exe in (("OFB-EAST__46851", sdk_game), ("OFB-EAST__57289", plain_game)):
        write_record(InstallRecord(
            game_id=gid, offer_id=gid.replace("__", ":"), content_id="1", install_path=str(home),
            exe=str(exe), build_id="/b.zip", version="1",
        ))

    assert standin.wants_standin("OFB-EAST__46851", sdk_game) is True
    assert standin.wants_standin("OFB-EAST__57289", plain_game) is False
    kept = read_record("OFB-EAST__57289")
    assert kept is not None and (kept.sdk_exe, kept.origin_sdk) == (str(plain_game), "no")

    # Measured once per exe: the record answers without reading the file again ...
    plain_game.write_bytes(b"MZ OriginSDK")
    assert standin.wants_standin("OFB-EAST__57289", plain_game) is False
    # ... and a different exe is measured afresh.
    other = home / "Crysis.exe"
    other.write_bytes(b"MZ originsdk")
    assert standin.wants_standin("OFB-EAST__57289", other) is True

    # A record written before these fields existed still loads.
    old = records_dir() / "Origin.OFR.50.1.json"
    old.write_text(json.dumps({"game_id": "Origin.OFR.50.1", "offer_id": "Origin.OFR.50.1", "content_id": "1",
                               "install_path": "/g", "exe": "/g/G.exe", "build_id": "/b", "version": "1"}))
    loaded = read_record("Origin.OFR.50.1")
    assert loaded is not None and loaded.origin_sdk == ""
