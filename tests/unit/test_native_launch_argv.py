"""Regression: native-launch argv must route GOG DOSBox titles correctly.

``services/launcher/orchestrator.py::launch_native`` is the live dispatch
target for native Linux games (``LauncherService`` calls it directly) —
but until this fix it built a bare ``[str(ctx.exe_path)]`` argv with no
DOSBox awareness at all, because the DOSBox-dispatch logic lived only in
``launcher/flows/native.py``, a module nothing ever imported. These tests
cover the ported ``build_native_argv`` in its live location
(``services/launcher/helpers.py``) directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

from unifideck.launcher.types.context import LaunchContext, RuntimeState
from unifideck.services.launcher.helpers import build_native_argv, prepare_native_env


def _ctx(store: str, exe_path: Path) -> LaunchContext:
    return LaunchContext(
        store=store,
        game_id="123",
        exe_path=exe_path,
        work_dir=exe_path.parent,
        plugin_dir=Path("/plugin"),
    )


def test_gog_start_sh_routes_through_dosbox_module(tmp_path):
    start_sh = tmp_path / "start.sh"
    start_sh.write_text("#!/bin/bash\n")
    ctx = _ctx("gog", start_sh)
    state = RuntimeState()

    argv = build_native_argv(ctx, state, ctx.exe_path)

    assert argv == [
        sys.executable, "-m",
        "unifideck.launcher.proton.handlers.gog_linux_dosbox",
        str(start_sh),
    ]


def test_gog_start_sh_module_path_is_importable():
    # Regression for the exact class of bug just fixed: the dispatch
    # argv must name a module path that actually resolves, not the
    # stale ``unifideck.launcher.proton.gog_linux_dosbox`` (missing
    # the ``.handlers.`` package) that shipped silently broken.
    import importlib

    importlib.import_module(
        "unifideck.launcher.proton.handlers.gog_linux_dosbox",
    )


def test_non_gog_native_game_execs_directly_without_steam_runtime(
    tmp_path, monkeypatch,
):
    # No Steam Runtime present on this machine/test env.
    monkeypatch.setattr(
        "unifideck.services.launcher.helpers.find_steam_runtime",
        lambda: None,
    )
    exe = tmp_path / "game.x86_64"
    exe.write_text("")
    ctx = _ctx("epic", exe)
    state = RuntimeState()

    argv = build_native_argv(ctx, state, ctx.exe_path)

    assert argv == [str(exe)]


def test_gog_non_start_sh_exe_does_not_use_dosbox_module(tmp_path):
    # A GOG native game that isn't a DOSBox title (no start.sh) must
    # not be routed through the DOSBox handler.
    exe = tmp_path / "game.x86_64"
    exe.write_text("")
    ctx = _ctx("gog", exe)
    state = RuntimeState()

    argv = build_native_argv(ctx, state, ctx.exe_path)

    assert "gog_linux_dosbox" not in " ".join(argv)


def test_game_args_are_appended(tmp_path):
    """Game arguments land at the end of argv.

    This test also asserted a leading ``gamemoderun`` from
    ``RuntimeState.wrappers``. That field is gone: Steam applies wrapper
    words pre-exec, so by the time the launcher holds an argv there is
    nothing to wrap, and six argv builders were prepending a list that could
    only ever be empty (audit register item 23b).
    """
    exe = tmp_path / "game.x86_64"
    exe.write_text("")
    ctx = _ctx("epic", exe)
    state = RuntimeState(game_args=["-fullscreen"])

    argv = build_native_argv(ctx, state, ctx.exe_path)

    assert argv[-1] == "-fullscreen"
    assert not hasattr(state, "wrappers")


def test_prepare_native_env_sets_pythonpath_to_plugin_py_modules(tmp_path):
    # Regression: ``unifideck-launcher`` makes the ``unifideck`` package
    # importable purely via an in-process sys.path.insert, which is
    # invisible to a child ``python3 -m ...`` subprocess (the DOSBox
    # wrapper dispatch). Without PYTHONPATH set explicitly here, that
    # child dies with ModuleNotFoundError within milliseconds — silently,
    # since nothing captured its stderr — which is exactly what broke
    # every real GOG DOSBox launch once this dispatch path went live.
    exe = tmp_path / "start.sh"
    exe.write_text("")
    plugin_dir = tmp_path / "plugin"
    ctx = LaunchContext(
        store="gog",
        game_id="123",
        exe_path=exe,
        work_dir=tmp_path,
        plugin_dir=plugin_dir,
    )

    env = prepare_native_env(ctx)

    assert env["PYTHONPATH"].split(":")[0] == str(plugin_dir / "py_modules")


def test_prepare_native_env_preserves_existing_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/some/other/path")
    exe = tmp_path / "start.sh"
    exe.write_text("")
    plugin_dir = tmp_path / "plugin"
    ctx = LaunchContext(
        store="gog",
        game_id="123",
        exe_path=exe,
        work_dir=tmp_path,
        plugin_dir=plugin_dir,
    )

    env = prepare_native_env(ctx)

    parts = env["PYTHONPATH"].split(":")
    assert parts[0] == str(plugin_dir / "py_modules")
    assert "/some/other/path" in parts
