"""EA cloud saves: the ``maxima-cli`` cloud commands and ``EACloudSaveStrategy``.

The CLI half runs a fake ``maxima-cli`` script that records its arguments and
environment. The strategy half swaps in a stub CLI, so nothing here talks to
EA or writes a real prefix.
"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from unifideck.core.store_capabilities import CLOUD_SAVE_STORES
from unifideck.services.cloud_save.ea_strategy import EACloudSaveStrategy
from unifideck.services.cloud_save.safety import SaveConflictError
from unifideck.stores.ea.cli import MaximaCli, MaximaCliError

_FAKE = """#!{python}
import json, os, sys
open({record!r}, "w").write(json.dumps({{
    "argv": sys.argv[1:],
    "env": {{k: v for k, v in os.environ.items() if k.startswith("MAXIMA_")}},
}}))
sys.stdout.write({stdout!r})
sys.exit({rc})
"""

GAME = "OFB-EAST__57289"
OFFER = "OFB-EAST:57289"


def _fake_cli(tmp_path: Path, *, stdout: str = "{}\n", rc: int = 0) -> tuple[str, Path]:
    record = tmp_path / "record.json"
    script = tmp_path / "maxima-cli"
    script.write_text(_FAKE.format(python=sys.executable, record=str(record), stdout=stdout, rc=rc))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), record


# ── MaximaCli ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("write", "force", "tail"),
    [(False, False, []), (True, False, ["--write"]), (False, True, ["--force"])],
)
async def test_cloud_sync_args_and_prefix(
    tmp_path: Path, write: bool, force: bool, tail: list[str],
) -> None:
    cli_path, record = _fake_cli(tmp_path, stdout='{"mode":"read","conflicts":0}\n')
    prefix = tmp_path / "prefix"

    result = await MaximaCli(cli_path, data_dir=tmp_path / "data").cloud_sync(
        OFFER, prefix, write=write, force=force,
    )

    seen = json.loads(record.read_text())
    assert seen["argv"] == ["--json", "cloud-sync", "--offer-id", OFFER, *tail]
    assert seen["env"]["MAXIMA_WINE_PREFIX"] == str(prefix)
    # The prefix is added to the usual env, never instead of it
    assert seen["env"]["MAXIMA_PACKAGED"] == "1"
    assert result["conflicts"] == 0


async def test_cloud_paths_reads_the_rules(tmp_path: Path) -> None:
    out = {"has_cloud_save": True, "folders": ["/p/drive_c/x"], "rules": ["%Documents%/x/"]}
    cli_path, record = _fake_cli(tmp_path, stdout=json.dumps(out) + "\n")

    result = await MaximaCli(cli_path, data_dir=tmp_path / "data").cloud_paths(OFFER, tmp_path)

    assert json.loads(record.read_text())["argv"] == ["cloud-paths", "--offer-id", OFFER]
    assert result == out


async def test_cloud_sync_failure_raises(tmp_path: Path) -> None:
    cli_path, _ = _fake_cli(tmp_path, stdout="", rc=1)
    with pytest.raises(MaximaCliError) as err:
        await MaximaCli(cli_path, data_dir=tmp_path / "data").cloud_sync(
            OFFER, tmp_path, write=True,
        )
    assert err.value.code == "command_failed"


# ── EACloudSaveStrategy ──────────────────────────────────────────────

class _StubCli:
    """Answers ``cloud-paths`` and ``cloud-sync`` from fixed values."""

    def __init__(
        self, paths: dict[str, Any] | None = None, sync: dict[str, Any] | Exception | None = None,
    ) -> None:
        self.paths = paths or {}
        self.sync = sync if sync is not None else {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def cloud_paths(self, offer: str, prefix: Path) -> dict[str, Any]:
        self.calls.append(("cloud_paths", {"offer": offer, "prefix": prefix}))
        return self.paths

    async def cloud_sync(
        self, offer: str, prefix: Path, *, write: bool, force: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("cloud_sync", {"offer": offer, "write": write, "force": force}))
        if isinstance(self.sync, Exception):
            raise self.sync
        return self.sync


def _strategy(tmp_path: Path, cli: _StubCli, *, prefix: bool = True) -> EACloudSaveStrategy:
    if prefix:
        (tmp_path / "prefixes" / GAME / "drive_c").mkdir(parents=True)
    strategy = EACloudSaveStrategy(str(tmp_path / "saves"))
    strategy._cli = cli  # type: ignore[assignment]
    return strategy


def test_ea_is_a_cloud_save_store() -> None:
    assert "ea" in CLOUD_SAVE_STORES


async def test_save_dir_is_the_first_rule_folder(tmp_path: Path) -> None:
    cli = _StubCli(paths={"has_cloud_save": True, "folders": ["/a", "/b"]})
    strategy = _strategy(tmp_path, cli)

    await strategy._prefetch_save_dir(GAME)

    assert strategy.get_local_save_dir(GAME) == "/a"
    assert cli.calls[0] == (
        "cloud_paths", {"offer": OFFER, "prefix": tmp_path / "prefixes" / GAME},
    )
    # Memoized: a second prefetch asks nothing
    await strategy._prefetch_save_dir(GAME)
    assert len(cli.calls) == 1


async def test_on_the_event_loop_the_cli_never_runs(tmp_path: Path) -> None:
    cli = _StubCli(paths={"has_cloud_save": True, "folders": ["/a"]})
    strategy = _strategy(tmp_path, cli)

    assert strategy._resolve_store_save_dir(GAME) is None
    assert cli.calls == []
    # A worker thread (the status page) may run it
    assert await asyncio.to_thread(strategy._resolve_store_save_dir, GAME) == "/a"


async def test_game_without_cloud_saves_syncs_nothing_and_succeeds(tmp_path: Path) -> None:
    cli = _StubCli(paths={"has_cloud_save": False})
    strategy = _strategy(tmp_path, cli)

    assert await strategy.sync_down(GAME) is True
    assert await strategy.sync_up(GAME) is True
    # One lookup, remembered; never a sync
    assert [name for name, _ in cli.calls] == ["cloud_paths"]


async def test_no_prefix_means_no_lookup(tmp_path: Path) -> None:
    cli = _StubCli(paths={"has_cloud_save": True, "folders": ["/a"]})
    strategy = _strategy(tmp_path, cli, prefix=False)

    await strategy._prefetch_save_dir(GAME)

    assert cli.calls == []


async def test_pull_conflict_keeps_local_and_succeeds(tmp_path: Path) -> None:
    cli = _StubCli(sync={"mode": "read", "downloaded": 0, "conflicts": 2})
    strategy = _strategy(tmp_path, cli)

    assert await strategy._do_sync_down(GAME, str(tmp_path), force=False) is True
    assert cli.calls[-1] == ("cloud_sync", {"offer": OFFER, "write": False, "force": False})


async def test_forced_pull_passes_force(tmp_path: Path) -> None:
    cli = _StubCli(sync={"mode": "read", "downloaded": 3, "conflicts": 0})
    strategy = _strategy(tmp_path, cli)

    assert await strategy._do_sync_down(GAME, str(tmp_path), force=True) is True
    assert cli.calls[-1][1]["force"] is True


async def test_push_conflict_raises_a_soft_conflict(tmp_path: Path) -> None:
    saves = tmp_path / "save"
    saves.mkdir()
    (saves / "slot1.sav").write_bytes(b"x")
    cli = _StubCli(sync={"mode": "write", "uploaded": 0, "conflicts": 1})
    strategy = _strategy(tmp_path, cli)

    with pytest.raises(SaveConflictError) as err:
        await strategy._do_sync_up(GAME, str(saves))

    assert err.value.hard is False
    assert err.value.store == "ea"
    assert cli.calls[-1][1]["write"] is True


async def test_push_without_conflict_succeeds(tmp_path: Path) -> None:
    cli = _StubCli(sync={"mode": "write", "uploaded": 2, "conflicts": 0})
    strategy = _strategy(tmp_path, cli)

    assert await strategy._do_sync_up(GAME, str(tmp_path)) is True


async def test_cli_failure_is_a_failed_sync(tmp_path: Path) -> None:
    cli = _StubCli(sync=MaximaCliError("command_failed", "cloud-sync exited 1"))
    strategy = _strategy(tmp_path, cli)

    assert await strategy._do_sync_down(GAME, str(tmp_path), force=False) is False
    assert await strategy._do_sync_up(GAME, str(tmp_path)) is False


async def test_cloud_status_args_and_prefix(tmp_path: Path) -> None:
    cli_path, record = _fake_cli(tmp_path, stdout='{"has_cloud_save":true,"cloud_files":0}\n')
    prefix = tmp_path / "prefix"
    await MaximaCli(cli_path, data_dir=tmp_path / "data").cloud_status(OFFER, prefix)
    seen = json.loads(record.read_text())
    assert seen["argv"] == ["cloud-status", "--offer-id", OFFER]
    assert seen["env"]["MAXIMA_WINE_PREFIX"] == str(prefix)


async def test_cloud_info_carries_the_file_verdict(tmp_path: Path) -> None:
    class _StatusCli(_StubCli):
        async def cloud_status(self, offer: str, prefix: Path) -> dict[str, Any]:
            return {
                "has_cloud_save": True, "cloud_files": 0, "cloud_bytes": 0,
                "cloud_modified": None, "local_only": 2,
            }

    strategy = _strategy(tmp_path, _StatusCli())
    info = await strategy._fetch_cloud_info(GAME)
    assert info == {
        "has_saves": False, "timestamp": 0.0, "file_count": 0,
        "total_bytes": 0, "verdict": "local_newer",
    }
    assert await strategy.sync_verdict(GAME, info) == "local_newer"


@pytest.mark.parametrize("force", [False, True])
async def test_push_passes_force_to_the_cli(tmp_path: Path, force: bool) -> None:
    cli = _StubCli(sync={"mode": "write", "uploaded": 1, "conflicts": 0})
    strategy = _strategy(tmp_path, cli)
    assert await strategy._do_sync_up(GAME, str(tmp_path), force=force) is True
    assert cli.calls[-1] == ("cloud_sync", {"offer": OFFER, "write": True, "force": force})
