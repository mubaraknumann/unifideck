"""Tests for ``stores.ea.cli`` and ``stores.ea.ids``: how the plugin runs maxima-cli.

A fake ``maxima-cli`` (a small Python script per test) records its arguments,
environment and working directory, and prints what each test asks for.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from unifideck.stores.ea.cli import MaximaCli, MaximaCliError
from unifideck.stores.ea.ids import offer_id, row_id

_FAKE = """#!{python}
import json, os, sys
open({record!r}, "w").write(json.dumps({{
    "argv": sys.argv[1:], "cwd": os.getcwd(), "cwd_files": os.listdir("."),
    "env": {{k: v for k, v in os.environ.items() if k.startswith("MAXIMA_")}},
}}))
sys.stderr.write({stderr!r})
sys.stdout.write({stdout!r})
sys.exit({rc})
"""


def _fake_cli(tmp_path: Path, *, stdout: str = "", stderr: str = "", rc: int = 0) -> tuple[str, Path]:
    record = tmp_path / "record.json"
    script = tmp_path / "maxima-cli"
    script.write_text(_FAKE.format(
        python=sys.executable, record=str(record), stdout=stdout, stderr=stderr, rc=rc,
    ))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), record


@pytest.mark.parametrize("offer", ["Origin.OFR.50.0002540", "OFB-EAST:59474"])
def test_row_id_round_trips_and_is_a_valid_game_id(offer: str) -> None:
    from unifideck.core.types.identifiers import validate_game_id

    row = row_id(offer)
    validate_game_id(row)
    assert ":" not in row
    assert offer_id(row) == offer


async def test_commands_run_in_an_empty_private_folder_with_our_env(tmp_path: Path) -> None:
    cli_path, record = _fake_cli(tmp_path, stdout='{"signed_in":true,"user_id":"1"}\n')
    cli = MaximaCli(cli_path, data_dir=tmp_path / "data")

    assert await cli.signed_in() is True

    seen = json.loads(record.read_text())
    assert seen["argv"] == ["auth-status"]
    assert seen["cwd"] == str(tmp_path / "data" / "run")
    assert seen["cwd_files"] == []
    assert seen["env"]["MAXIMA_DATA_DIR"] == str(tmp_path / "data")
    assert seen["env"]["MAXIMA_PACKAGED"] == "1"
    assert seen["env"]["MAXIMA_DISABLE_QRC"] == "1"
    assert stat.S_IMODE(os.stat(tmp_path / "data").st_mode) == 0o700


async def test_auth_url_must_be_https(tmp_path: Path) -> None:
    cli_path, _ = _fake_cli(tmp_path, stdout='{"url":"qrc:/nope"}\n')
    with pytest.raises(MaximaCliError) as err:
        await MaximaCli(cli_path, data_dir=tmp_path / "data").auth_url()
    assert err.value.code == "bad_output"


async def test_auth_exchange_passes_the_code(tmp_path: Path) -> None:
    cli_path, record = _fake_cli(tmp_path, stdout='{"signed_in":true,"user_id":"42"}\n')
    user = await MaximaCli(cli_path, data_dir=tmp_path / "data").auth_exchange("CODE")
    assert user == "42"
    assert json.loads(record.read_text())["argv"] == ["auth-exchange", "--code", "CODE"]


async def test_not_signed_in_is_its_own_error(tmp_path: Path) -> None:
    cli_path, _ = _fake_cli(tmp_path, stderr="ERROR - [main.rs:150] - Not signed in: Not signed in\n", rc=1)
    with pytest.raises(MaximaCliError) as err:
        await MaximaCli(cli_path, data_dir=tmp_path / "data").list_games()
    assert err.value.code == "not_signed_in"


async def test_list_games_reads_the_last_json_line(tmp_path: Path) -> None:
    games = {"games": [{"offer_id": "OFB-EAST:59474", "name": "Game"}, "junk"]}
    cli_path, record = _fake_cli(tmp_path, stdout=f"warming up\n{json.dumps(games)}\n")
    listed = await MaximaCli(cli_path, data_dir=tmp_path / "data").list_games()
    assert listed == [{"offer_id": "OFB-EAST:59474", "name": "Game"}]
    assert json.loads(record.read_text())["argv"] == ["list-games", "--json"]


async def test_missing_binary_is_reported_not_raised_as_oserror(tmp_path: Path) -> None:
    with pytest.raises(MaximaCliError) as err:
        await MaximaCli(None, data_dir=tmp_path / "data").signed_in()
    assert err.value.code == "cli_missing"


async def test_build_size_reads_installed_bytes(tmp_path: Path) -> None:
    cli_path, record = _fake_cli(tmp_path, stdout='{"build_id":"/b.zip","download_bytes":7203881970,"install_bytes":7500000000}\n')
    cli = MaximaCli(cli_path, data_dir=tmp_path / "data")
    assert await cli.build_size("OFB-EAST:59474") == 7500000000
    assert json.loads(record.read_text())["argv"] == ["build-size", "--offer-id", "OFB-EAST:59474"]
