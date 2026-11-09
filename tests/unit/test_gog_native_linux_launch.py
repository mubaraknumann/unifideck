"""Regression: GOG Linux-native installs (Streets of Rage 4, GOG 1600715263).

Three faults, all seen in one tester's Capture Logs bundles:

* Save sync passed ``--os linux`` to ``gogdl save-sync``, which GOG's builds
  API rejects (covered in ``test_cloud_save.py``).
* The resolver preferred the goggame play task (``game/SOR4``) over the
  install's ``start.sh``, so we skipped the entry script upstream gogdl runs.
* A native game that died on start left no output, no exit code and no
  message: the game just vanished back to the library.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from unifideck.launcher import dispatcher as d
from unifideck.launcher.types.context import LaunchContext, RuntimeState
from unifideck.services.launcher.helpers import build_native_argv
from unifideck.services.shortcut.games_map import GameMapEntry
from unifideck.stores.gog.exe_resolver import GOGExeResolver


def _gog_linux_install(root: Path) -> Path:
    """A GOG Linux install: root start.sh plus a goggame play task."""
    (root / "game").mkdir(parents=True)
    (root / "game" / "SOR4").write_text("")
    (root / "game" / "goggame-1600715263.info").write_text(
        '{"playTasks": [{"isPrimary": true, "type": "FileTask", "path": "SOR4"}]}',
    )
    start_sh = root / "start.sh"
    start_sh.write_text("#!/bin/bash\n")
    return start_sh


# ── launch target ────────────────────────────────────────────────────────


def test_resolver_prefers_start_sh_over_play_task(tmp_path):
    start_sh = _gog_linux_install(tmp_path)
    assert GOGExeResolver().find_with_workdir(str(tmp_path)) == (
        str(start_sh), str(tmp_path),
    )


def test_resolver_still_uses_play_task_without_start_sh(tmp_path):
    _gog_linux_install(tmp_path)
    (tmp_path / "start.sh").unlink()
    exe = GOGExeResolver().find(str(tmp_path))
    assert exe is not None and Path(exe).name == "SOR4"


class _ShortcutSvc:
    def __init__(self, entry: GameMapEntry) -> None:
        self._entry = entry

    async def get_entry_for_game_key(self, _store, _game_id):
        return self._entry


async def test_dispatcher_swaps_old_play_task_row_for_start_sh(tmp_path):
    start_sh = _gog_linux_install(tmp_path)
    svc = _ShortcutSvc(GameMapEntry(str(tmp_path / "game" / "SOR4"), str(tmp_path), -1))
    exe, work_dir, has_entry, _ = await d._resolve_game_exe(
        svc, "gog", "1600715263", "gog:1600715263",
    )
    assert (exe, work_dir, has_entry) == (str(start_sh), str(tmp_path), True)


async def test_dispatcher_leaves_other_stores_alone(tmp_path):
    _gog_linux_install(tmp_path)
    binary = str(tmp_path / "game" / "SOR4")
    svc = _ShortcutSvc(GameMapEntry(binary, str(tmp_path), -1))
    exe, *_ = await d._resolve_game_exe(svc, "itch", "1", "itch:1")
    assert exe == binary


# ── argv: start.sh alone is not a DOSBox title ───────────────────────────


def _ctx(exe: Path, store: str = "gog") -> LaunchContext:
    return LaunchContext(
        store=store, game_id="1600715263", exe_path=exe,
        work_dir=exe.parent, plugin_dir=Path("/plugin"),
    )


def test_start_sh_without_dosbox_runs_under_steam_runtime(tmp_path, monkeypatch):
    run_sh = tmp_path / "run.sh"
    monkeypatch.setattr(
        "unifideck.services.launcher.helpers.find_steam_runtime", lambda: run_sh,
    )
    start_sh = _gog_linux_install(tmp_path / "install")
    argv = build_native_argv(_ctx(start_sh), RuntimeState(), start_sh)
    assert argv == [str(run_sh), str(start_sh)]


def test_start_sh_with_dosbox_dir_uses_dosbox_module(tmp_path):
    start_sh = tmp_path / "start.sh"
    start_sh.write_text("#!/bin/bash\n")
    (tmp_path / "dosbox").mkdir()
    argv = build_native_argv(_ctx(start_sh), RuntimeState(), start_sh)
    assert "unifideck.launcher.proton.handlers.gog_linux_dosbox" in argv


# ── run: output on disk, early exit reported ─────────────────────────────


def _script(tmp_path: Path, body: str) -> Path:
    exe = tmp_path / "game.sh"
    exe.write_text("#!/bin/sh\n" + body)  # run_native_subprocess sets +x
    return exe


async def _run(tmp_path, monkeypatch, body: str):
    from unifideck.launcher.proton.infrastructure.game_log import game_log_path
    from unifideck.services.launcher.native_run import run_native_subprocess

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(
        "unifideck.services.launcher.helpers.find_steam_runtime", lambda: None,
    )
    toast = AsyncMock()
    monkeypatch.setattr("unifideck.launcher.rpc.emit_stage", toast)
    svc = SimpleNamespace(_bus=MagicMock(), _active_subprocess=None)
    exe = _script(tmp_path, body)
    rc = await run_native_subprocess(svc, _ctx(exe, store="epic"), RuntimeState())
    log = game_log_path()
    assert log is not None
    return rc, log.read_text(), toast


async def test_native_output_lands_in_game_log_and_early_exit_toasts(
    tmp_path, monkeypatch,
):
    rc, log, toast = await _run(
        tmp_path, monkeypatch, "echo hello-from-game\necho oops >&2\nexit 3\n",
    )
    assert rc == 3
    assert "hello-from-game" in log and "oops" in log
    toast.assert_awaited_once()
    kwargs = toast.await_args.kwargs
    assert kwargs["i18n_key"] == "toasts.launcher.nativeExitedEarly"
    assert kwargs["i18n_title_key"] == "toasts.header.gameLaunch"


async def test_clean_native_exit_does_not_toast(tmp_path, monkeypatch):
    rc, _, toast = await _run(tmp_path, monkeypatch, "exit 0\n")
    assert rc == 0
    toast.assert_not_awaited()


def test_dosbox_argv_never_starts_with_an_empty_interpreter(tmp_path, monkeypatch):
    # A real Steam launch had sys.executable == "", so the DOSBox wrapper
    # spawn exec'd "" and crashed before DOSBox started.
    monkeypatch.setattr("sys.executable", "")
    start_sh = tmp_path / "start.sh"
    start_sh.write_text("#!/bin/bash\n")
    (tmp_path / "dosbox").mkdir()
    argv = build_native_argv(_ctx(start_sh), RuntimeState(), start_sh)
    assert argv[0] and Path(argv[0]).is_file()


def test_dosbox_runtime_skips_the_library_less_stub(tmp_path, monkeypatch):
    # Current Steam ships ubuntu12_32/steam-runtime as a stub with no libs;
    # the scout libs DOSBox needs live in the Steam Linux Runtime 1.0 tool.
    from unifideck.launcher.proton.handlers import gog_linux_dosbox as dosbox

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    steam = tmp_path / ".local" / "share" / "Steam"
    (steam / "ubuntu12_32" / "steam-runtime" / "usr" / "libexec").mkdir(parents=True)
    slr = steam / "steamapps" / "common" / "SteamLinuxRuntime" / "steam-runtime"
    (slr / "usr" / "lib" / "x86_64-linux-gnu").mkdir(parents=True)
    assert dosbox.find_steam_runtime() == slr
