"""The Ubisoft launch's registry writers must never invoke Proton's wine.

Regression guard for the shared-Proton corruption reported from the field
(Legion Go S, dedicated SteamOS, 0.7.4, GE-Proton11-6): every Ubisoft launch
left ``kernel32.dll`` / ``win32u.dll`` / ``user32.dll`` in
``compatibilitytools.d/GE-Proton11-6`` truncated, breaking every game of every
store until the Proton install was deleted and redownloaded.

The mechanism is the reason these tests exist. Proton builds a prefix whose
``drive_c/windows/system32/*.dll`` entries are SYMLINKS back into the shared
Proton install::

    kernel32.dll -> <PROTONPATH>/files/lib/wine/x86_64-windows/kernel32.dll

A bare ``wine`` against that prefix runs a full ``wineboot`` update and
reinstalls every builtin PE DLL into ``system32`` — through those symlinks,
into the shared Proton tree, source and destination being the same file. Two
sites did exactly that on every Ubisoft launch (``epic_prefix_fix``, one key;
``epic_registry``, up to seven). Both now go through umu.
"""
from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

from unifideck.launcher.proton.fixes import epic_prefix_fix, epic_registry
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan


def _plan(prefix: Path) -> ProtonLaunchPlan:
    return ProtonLaunchPlan(
        context=types.SimpleNamespace(
            game_id="abc123", store="ubisoft",
            exe_path=prefix / "game.exe",
            work_dir=prefix,
            plugin_dir=Path("/plugin"),
        ),
        state=types.SimpleNamespace(wrappers=[], game_args=[], umu_id=None),
        python_bin=Path("/usr/bin/python3"),
        umu_wrapper=Path("/plugin/bin/umu/umu/umu-run"),
        prefix_path=prefix,
        env={"PROTONPATH": "/proton/GE-Proton11-6",
             "STEAM_COMPAT_DATA_PATH": str(prefix)},
    )


class _Recorder:
    """Stands in for ``run_setup_exe`` and records every spawn."""

    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._ok = ok

    async def __call__(self, plan, exe, args, **kwargs):
        self.calls.append((exe, list(args)))
        return self._ok


def _make_prefix(tmp_path: Path) -> Path:
    (tmp_path / "drive_c" / "windows").mkdir(parents=True)
    return tmp_path


# ── the core guard: nothing spawns a Proton wine binary ───────────


@pytest.mark.asyncio
async def test_epic_prefix_fix_never_spawns_proton_wine(tmp_path, monkeypatch):
    prefix = _make_prefix(tmp_path)
    wrapper = tmp_path / "EpicGamesLauncher.exe"
    wrapper.write_bytes(b"MZ")
    rec = _Recorder()
    monkeypatch.setattr(epic_prefix_fix, "run_setup_exe", rec)

    assert await epic_prefix_fix.apply_epic_launcher_fix(
        plan=_plan(prefix), prefix_path=prefix, bundled_wrapper=wrapper,
    ) is True

    assert rec.calls == [
        ("reg.exe", ["add", r"HKEY_CLASSES_ROOT\com.epicgames.launcher", "/f"]),
    ]
    # Single backslash: the old code double-escaped it and Wine saw a literal
    # ``\\`` in the key name.
    assert r"\\com" not in rec.calls[0][1][1]


@pytest.mark.asyncio
async def test_epic_registry_never_spawns_proton_wine(tmp_path, monkeypatch):
    prefix = _make_prefix(tmp_path)
    config = tmp_path / "legendary"
    config.mkdir()
    (config / "installed.json").write_text(
        '{"abc123": {"install_path": "/games/wd2",'
        ' "launch_parameters": "-UplayId= 3619"}}',
    )
    rec = _Recorder()
    monkeypatch.setattr(epic_registry, "run_setup_exe", rec)

    result = await epic_registry.setup_registry(
        plan=_plan(prefix), game_id="abc123", legendary_config=config,
    )

    assert result.success is True
    assert result.keys_written == 5
    assert {exe for exe, _ in rec.calls} == {"reg.exe"}
    for _exe, args in rec.calls:
        assert not any(a.endswith("/files/bin/wine") for a in args)
        assert args[0] == "add"


def test_neither_fix_can_reach_a_wine_binary_or_wineserver():
    """The wine-resolving helpers are gone, not merely unused.

    Compares CODE, not source text: both modules' docstrings name
    ``files/bin/wine`` on purpose, to record why they must never run it.
    """
    for module in (epic_prefix_fix, epic_registry):
        assert not hasattr(module, "_find_wine_binary")
        assert not hasattr(module, "kill_wineserver")
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "files/bin/wine" not in node.value or _is_docstring(
                    node, tree,
                )


def _is_docstring(node: ast.Constant, tree: ast.Module) -> bool:
    """Whether ``node`` is the docstring of the module or of a def in it."""
    scopes: list[ast.AST] = [tree]
    scopes += [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    return any(
        scope.body
        and isinstance(scope.body[0], ast.Expr)
        and scope.body[0].value is node
        for scope in scopes
    )


# ── behaviour preserved ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_writes_both_wow64_views_and_uplay_keys(
    tmp_path, monkeypatch,
):
    prefix = _make_prefix(tmp_path)
    config = tmp_path / "legendary"
    config.mkdir()
    (config / "installed.json").write_text(
        '{"abc123": {"install_path": "/games/wd2",'
        ' "launch_parameters": "-UplayId= 3619"}}',
    )
    rec = _Recorder()
    monkeypatch.setattr(epic_registry, "run_setup_exe", rec)
    await epic_registry.setup_registry(
        plan=_plan(prefix), game_id="abc123", legendary_config=config,
    )

    keys = [args[1] for _exe, args in rec.calls]
    assert any("WOW6432Node\\Epic Games" in k for k in keys)
    assert any(k.startswith("HKEY_CURRENT_USER\\Software\\Epic Games") for k in keys)
    assert sum("Ubisoft\\Launcher\\Installs\\3619" in k for k in keys) == 2
    # The install path reaches the registry as a Z: path with a trailing sep.
    assert any("Z:\\games\\wd2\\" in args for _exe, args in rec.calls)


@pytest.mark.asyncio
async def test_registry_omits_uplay_keys_without_an_id(tmp_path, monkeypatch):
    prefix = _make_prefix(tmp_path)
    config = tmp_path / "legendary"
    config.mkdir()
    (config / "installed.json").write_text(
        '{"abc123": {"install_path": "/games/wd2", "launch_parameters": ""}}',
    )
    rec = _Recorder()
    monkeypatch.setattr(epic_registry, "run_setup_exe", rec)
    await epic_registry.setup_registry(
        plan=_plan(prefix), game_id="abc123", legendary_config=config,
    )
    assert len(rec.calls) == 3
    assert not any("Ubisoft" in args[1] for _exe, args in rec.calls)


@pytest.mark.asyncio
async def test_prefix_fix_is_non_fatal_when_the_registry_step_fails(
    tmp_path, monkeypatch,
):
    """A failed key must not fail the launch — the wrapper copy is the point."""
    prefix = _make_prefix(tmp_path)
    wrapper = tmp_path / "EpicGamesLauncher.exe"
    wrapper.write_bytes(b"MZ")
    monkeypatch.setattr(epic_prefix_fix, "run_setup_exe", _Recorder(ok=False))

    assert await epic_prefix_fix.apply_epic_launcher_fix(
        plan=_plan(prefix), prefix_path=prefix, bundled_wrapper=wrapper,
    ) is True
    copied = (
        prefix / "drive_c" / "Program Files (x86)" / "Epic Games" / "Launcher"
        / "Portal" / "Binaries" / "Win32" / "EpicGamesLauncher.exe"
    )
    assert copied.is_file()


@pytest.mark.asyncio
async def test_prefix_fix_skips_an_uninitialised_prefix(tmp_path, monkeypatch):
    wrapper = tmp_path / "EpicGamesLauncher.exe"
    wrapper.write_bytes(b"MZ")
    rec = _Recorder()
    monkeypatch.setattr(epic_prefix_fix, "run_setup_exe", rec)
    empty = tmp_path / "no-prefix"
    empty.mkdir()

    assert await epic_prefix_fix.apply_epic_launcher_fix(
        plan=_plan(empty), prefix_path=empty, bundled_wrapper=wrapper,
    ) is False
    assert rec.calls == []


# ── the symlink hazard itself ─────────────────────────────────────


@pytest.mark.asyncio
async def test_shared_proton_dlls_survive_the_fix(tmp_path, monkeypatch):
    """Model the field failure: system32 symlinked into a shared Proton tree.

    Before the fix, this ran a Proton ``wine`` against the prefix, Wine's
    prefix update rewrote ``system32/kernel32.dll``, and the write landed on
    the symlink's target inside the shared Proton install.
    """
    proton = tmp_path / "GE-Proton11-6" / "files" / "lib" / "wine" / "x86_64-windows"
    proton.mkdir(parents=True)
    real_dll = proton / "kernel32.dll"
    real_dll.write_bytes(b"MZ" + b"\x00" * 4096)
    before = real_dll.read_bytes()

    prefix = _make_prefix(tmp_path / "prefix")
    system32 = prefix / "drive_c" / "windows" / "system32"
    system32.mkdir(parents=True)
    (system32 / "kernel32.dll").symlink_to(real_dll)

    wrapper = tmp_path / "EpicGamesLauncher.exe"
    wrapper.write_bytes(b"MZ")
    monkeypatch.setattr(epic_prefix_fix, "run_setup_exe", _Recorder())
    await epic_prefix_fix.apply_epic_launcher_fix(
        plan=_plan(prefix), prefix_path=prefix, bundled_wrapper=wrapper,
    )

    assert real_dll.read_bytes() == before
    assert real_dll.stat().st_size == 4098
