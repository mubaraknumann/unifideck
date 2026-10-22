"""VC++ for Xbox PC prefixes: detection, the installer, the install and the runner (I-047)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc import prefix_vcredist as vc
from unifideck.launcher.xbox_pc.errors import (
    NOTICE_TITLES,
    VC_RUNTIME_FAILED_KEY,
    VC_RUNTIME_INSTALLING_KEY,
    VC_RUNTIME_TITLE_KEY,
)
from unifideck.launcher.xbox_pc.game_manifest import inspect_game
from unifideck.launcher.xbox_pc.runner import NO_WAIT_FLAG, write_runner
from unifideck.launcher.xbox_pc.runtime import Runtime

CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Game configVersion="1">
  <Identity Name="Test.Game" Publisher="CN=Test" Version="1.0.0.0"/>
  <ExecutableList><Executable Name="Game.exe" Id="Game"/></ExecutableList>
  {extra}
</Game>
"""
APPX = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">
  <Dependencies>
    <PackageDependency Name="{name}" MinVersion="14.0.33728.0" Publisher="CN=Microsoft"/>
  </Dependencies>
</Package>
"""
VC_SECTIONS = {
    "x64": "[Software\\\\Microsoft\\\\VisualStudio\\\\14.0\\\\VC\\\\Runtimes\\\\x64] 1700000000\n",
    "x86": "[Software\\\\Wow6432Node\\\\Microsoft\\\\VisualStudio\\\\14.0\\\\VC\\\\Runtimes\\\\x86] 1700000000\n",
}
VC_VALUES = '"Installed"=dword:00000001\n"Version"="v14.51.36247.00"\n\n'
VC_SECTION = "WINE REGISTRY Version 2\n\n" + VC_SECTIONS["x64"] + VC_VALUES
_PATH = {"PATH": os.environ["PATH"]}


def _game(root: Path, *, config_extra: str = "", appx_dependency: str | None = None) -> Path:
    root.mkdir(parents=True)
    (root / "MicrosoftGame.config").write_text(CONFIG.format(extra=config_extra))
    if appx_dependency:
        (root / "AppxManifest.xml").write_text(APPX.format(name=appx_dependency))
    return root


def _installer(path: Path, head: bytes = b"MZ") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + b"\x00" * (1 << 20))
    return path


def _dll_dir(prefix: Path, arch: str = "x64") -> Path:
    folder = prefix / "drive_c/windows" / vc.VC_ARCHES[arch]
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _installing_proton(root: Path, *, rc: int = 0, registers: bool = True, native: bool = True) -> Runtime:
    """A runtime whose ``proton run <installer>`` records argv and acts like the redistributable.

    ``VC_redist.x86.exe`` registers under Wow6432Node and fills syswow64;
    any other installer acts as x64.
    """
    root.mkdir(parents=True)
    proton = root / "proton"
    proton.write_text("\n".join([
        f"#!{sys.executable}",
        "import json, os, pathlib, sys",
        "data = pathlib.Path(os.environ['STEAM_COMPAT_DATA_PATH'])",
        "with (data / 'calls.jsonl').open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')",
        "arch = 'x86' if 'x86' in pathlib.Path(sys.argv[2]).name else 'x64'",
        f"sections = {VC_SECTIONS!r}",
        f"if {registers}:",
        "    with (data / 'system.reg').open('a') as f:",
        f"        f.write(sections[arch] + {VC_VALUES!r})",
        f"folder = data / 'drive_c/windows' / {vc.VC_ARCHES!r}[arch]",
        "folder.mkdir(parents=True, exist_ok=True)",
        f"(folder / 'concrt140.dll').write_bytes(b'MZ' + (b'' if {native} else b'Wine builtin DLL'))",
        "print('installer output')",
        f"sys.exit({rc})",
    ]) + "\n")
    proton.chmod(0o755)
    return Runtime(root)


# ── detection ────────────────────────────────────────────────────────

def test_config_known_dependency_needs_vc(tmp_path: Path) -> None:
    game = _game(tmp_path / "g", config_extra='<KnownDependency Name="VC14"/>')
    assert inspect_game(game).needs_vc_runtime


def test_appx_vclibs_framework_needs_vc(tmp_path: Path) -> None:
    game = _game(tmp_path / "g", appx_dependency="Microsoft.VCLibs.140.00.UWPDesktop")
    assert inspect_game(game).needs_vc_runtime


def test_other_dependencies_do_not_need_vc(tmp_path: Path) -> None:
    game = _game(tmp_path / "g", appx_dependency="Microsoft.GamingServices")
    assert not inspect_game(game).needs_vc_runtime
    assert not inspect_game(_game(tmp_path / "h")).needs_vc_runtime


# ── the installer ────────────────────────────────────────────────────

@pytest.mark.parametrize("arch", ["x64", "x86"])
def test_steam_redist_is_found_by_what_it_is(tmp_path: Path, arch: str) -> None:
    relative = Path(vc.STEAM_REDIST_DIR) / vc.installer_name(arch)
    fake = tmp_path / "fake-lib"
    _installer(fake / relative, head=b"PK")
    real = tmp_path / "real-lib"
    _installer(real / relative)
    assert vc.steam_vc_redist([fake], arch) is None, "a file with the right name but no PE header is not used"
    assert vc.steam_vc_redist([fake, real], arch) == real / relative
    assert vc.steam_vc_redist([tmp_path / "empty"], arch) is None


def test_download_keeps_one_checked_copy_per_arch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    source = _installer(tmp_path / "remote.exe")
    first = vc.download_vc_redist("x86", source.as_uri())
    assert first.name == "VC_redist.x86.exe" and first.read_bytes() == source.read_bytes()
    source.unlink()
    assert vc.download_vc_redist("x86", source.as_uri()) == first, "a kept copy is not fetched again"
    assert vc.download_url("x64").endswith("/vc_redist.x64.exe")


def test_download_refuses_what_is_not_an_installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    page = tmp_path / "page.html"
    page.write_bytes(b"<html>" * 300000)
    with pytest.raises(OSError):
        vc.download_vc_redist("x64", page.as_uri())
    assert not list((tmp_path / "data/unifideck/xbox_pc/vcredist").iterdir()), "nothing partial is left"


# ── the prefix ───────────────────────────────────────────────────────

@pytest.mark.parametrize("arch", ["x64", "x86"])
def test_native_concrt_is_told_from_wines_builtin(tmp_path: Path, arch: str) -> None:
    dll = _dll_dir(tmp_path, arch) / "concrt140.dll"
    assert not vc.native_vc_present(tmp_path, arch)
    dll.write_bytes(b"MZ" + b"\x00" * 64 + b"Wine builtin DLL")
    assert not vc.native_vc_present(tmp_path, arch)
    dll.write_bytes(b"MZ" + b"\x00" * 64)
    assert vc.native_vc_present(tmp_path, arch)


def test_ready_needs_the_registration_and_the_dlls_of_that_arch(tmp_path: Path) -> None:
    (_dll_dir(tmp_path, "x64") / "concrt140.dll").write_bytes(b"MZ")
    (_dll_dir(tmp_path, "x86") / "concrt140.dll").write_bytes(b"MZ")
    assert not vc.vc_runtime_ready(tmp_path, "x64")
    (tmp_path / "system.reg").write_text(VC_SECTION)
    assert vc.vc_runtime_ready(tmp_path, "x64")
    assert not vc.vc_runtime_ready(tmp_path, "x86"), "x64's key says nothing about x86"
    (tmp_path / "system.reg").write_text(VC_SECTION + VC_SECTIONS["x86"] + VC_VALUES)
    assert vc.vc_runtime_ready(tmp_path, "x86")


@pytest.mark.asyncio
@pytest.mark.parametrize("arch", ["x64", "x86"])
async def test_install_runs_the_installer_quietly(tmp_path: Path, arch: str) -> None:
    runtime = _installing_proton(tmp_path / "runtime")
    compat = tmp_path / "pfx"
    compat.mkdir()
    installer = _installer(tmp_path / vc.installer_name(arch))
    env = {"STEAM_COMPAT_DATA_PATH": str(compat), **_PATH}
    assert await vc.install_vc_runtime(runtime, compat, env, installer, arch, timeout_s=30.0) is None
    calls = [json.loads(line) for line in (compat / "calls.jsonl").read_text().splitlines()]
    assert calls == [["run", str(installer), "/install", "/quiet", "/norestart"]]
    assert vc.vc_runtime_ready(compat, arch)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kwargs", "expected"), [
    # 1603 (fatal error) reaches Linux as its low byte.
    ({"rc": 1603 & 0xFF, "registers": False}, "exited 67"),
    ({"registers": False}, "unregistered"),
    ({"native": False}, "Wine's concrt140.dll in system32"),
])
async def test_install_failures_say_why(tmp_path: Path, kwargs: dict[str, object], expected: str) -> None:
    runtime = _installing_proton(tmp_path / "runtime", **kwargs)  # type: ignore[arg-type]
    compat = tmp_path / "pfx"
    compat.mkdir()
    env = {"STEAM_COMPAT_DATA_PATH": str(compat), **_PATH}
    installer = _installer(tmp_path / vc.installer_name("x64"))
    problem = await vc.install_vc_runtime(runtime, compat, env, installer, "x64", timeout_s=30.0)
    assert problem is not None and expected in problem


@pytest.mark.asyncio
async def test_a_newer_runtime_already_there_counts_as_installed(tmp_path: Path) -> None:
    # 1638 means a newer VC++ is installed; Linux sees its low byte, 102.
    runtime = _installing_proton(tmp_path / "runtime", rc=1638 & 0xFF)
    compat = tmp_path / "pfx"
    compat.mkdir()
    env = {"STEAM_COMPAT_DATA_PATH": str(compat), **_PATH}
    installer = _installer(tmp_path / vc.installer_name("x64"))
    assert await vc.install_vc_runtime(runtime, compat, env, installer, "x64", timeout_s=30.0) is None


# ── the toasts ───────────────────────────────────────────────────────

def test_vc_notices_split_between_title_and_sub_text() -> None:
    assert NOTICE_TITLES[VC_RUNTIME_INSTALLING_KEY] == VC_RUNTIME_TITLE_KEY
    assert NOTICE_TITLES[VC_RUNTIME_FAILED_KEY] == VC_RUNTIME_TITLE_KEY


# ── the runner ───────────────────────────────────────────────────────

def test_runner_loads_microsofts_vc_first_and_the_user_still_wins(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    bin_dir = root / "files/bin"
    bin_dir.mkdir(parents=True)
    for name in ("wine", "wineserver"):
        (bin_dir / name).write_text(f'#!/bin/sh\necho "overrides=$WINEDLLOVERRIDES" >> {calls}\n')
        (bin_dir / name).chmod(0o755)
    runner = write_runner(Runtime(root), tmp_path, nvapi=True, native_vc=True)
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True, env={**_PATH, "WINEDLLOVERRIDES": "concrt140=b"})
    line = calls.read_text()
    assert "vcruntime140,vcruntime140_1=n,b;concrt140=b" in line, "the user's override comes last"
    calls.unlink()
    subprocess.run([str(write_runner(Runtime(root), tmp_path, nvapi=True)), NO_WAIT_FLAG, "x"],
                   check=True, env=_PATH)
    assert "concrt140" not in calls.read_text()
