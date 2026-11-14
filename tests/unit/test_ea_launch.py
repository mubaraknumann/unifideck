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
    assert "licence requested" in (home / ".local/share/unifideck/ea/maxima/launch-prepare.log").read_text()


async def test_sidecar_that_exits_early_reports_its_log(home: Path) -> None:
    cli = _script(home, _FAKE_FAILS)
    with pytest.raises(sidecar_mod.SidecarError) as err:
        await sidecar_mod.start_sidecar(cli, "OFB-EAST:59474", home / "Game.exe", {"PATH": "/usr/bin:/bin"})
    assert err.value.returncode == 1
    assert "Not signed in" in err.value.detail


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
    running = MagicMock(env={"EALsxPort": "3216"}, stop=AsyncMock())
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
