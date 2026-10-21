"""Game manifest and prefix helpers (ported from the launch spike).

``InspectTests`` and ``PrefixTests`` of ``xbox-pc-spike/launch/test_launch.py``,
plus the prefix creation step, which the spike ran with ``subprocess`` and
the launcher now runs on the event loop with a fake ``proton``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc import prefix as prefix_mod
from unifideck.launcher.xbox_pc.errors import XboxPcLaunchError
from unifideck.launcher.xbox_pc.game_manifest import inspect_game
from unifideck.launcher.xbox_pc.prefix_checks import (
    VC_REDIST_VERSION,
    installed_eac_runtime,
    installed_vc_runtime,
    vc_runtime_problem,
    version_at_least,
)
from unifideck.launcher.xbox_pc.runtime import Runtime

CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Game configVersion="1">
  <Identity Name="Studio.Game" Publisher="CN=Studio" Version="1.0.0.0"/>
  <StoreId>9NBLGGH4R315</StoreId>
  <TitleId>5A7B1C2D</TitleId>
  <ExecutableList><Executable Name="Game\\Binaries\\WinGDK\\Game.exe" Id="Game"/></ExecutableList>
  <ShellVisuals DefaultDisplayName="Sample Game" PublisherDisplayName="Studio"/>
  <Resources><Resource Language="en-US"/><Resource Language="fr-FR"/><Resource Language="not a locale"/></Resources>
  <KnownDependencyList><KnownDependency Name="VC14"/></KnownDependencyList>
</Game>
"""


# ── manifest (spike InspectTests) ────────────────────────────────────

def test_reads_manifest_case_insensitively(tmp_path: Path) -> None:
    (tmp_path / "MicrosoftGame.Config").write_text(CONFIG)
    redist = tmp_path / "Engine/Extras/Redist/en-us"
    redist.mkdir(parents=True)
    (redist / "vc_redist.x64.exe").write_bytes(b"\x00encrypted")
    game = inspect_game(tmp_path)
    assert game.title == "Sample Game"
    assert game.executable == "Game\\Binaries\\WinGDK\\Game.exe"
    assert game.exe_name == "Game.exe"
    assert game.store_id == "9NBLGGH4R315"
    assert game.title_id == "5A7B1C2D"
    assert game.locales == ("en-US", "fr-FR")
    assert game.vc_redist_encrypted
    assert not game.container_ready
    (tmp_path / ".xodus-streaming.msixvc").write_bytes(b"")
    assert inspect_game(tmp_path).container_ready


def test_plain_redist_is_not_encrypted(tmp_path: Path) -> None:
    (tmp_path / "MicrosoftGame.config").write_text(CONFIG)
    redist = tmp_path / "Engine/Extras/Redist/en-us"
    redist.mkdir(parents=True)
    (redist / "vc_redist.x64.exe").write_bytes(b"MZ\x90\x00")
    assert not inspect_game(tmp_path).vc_redist_encrypted


def test_missing_manifest_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(XboxPcLaunchError) as err:
        inspect_game(tmp_path)
    assert err.value.reason == "not_downloaded"


def test_broken_manifest_is_not_downloaded(tmp_path: Path) -> None:
    (tmp_path / "MicrosoftGame.config").write_text("<Game><unclosed>")
    with pytest.raises(XboxPcLaunchError) as err:
        inspect_game(tmp_path)
    assert err.value.reason == "not_downloaded"


# ── prefix (spike PrefixTests) ───────────────────────────────────────

def test_vc_runtime_version_from_system_reg(tmp_path: Path) -> None:
    (tmp_path / "system.reg").write_text(
        "WINE REGISTRY Version 2\n\n"
        "[Software\\\\Microsoft\\\\VisualStudio\\\\14.0\\\\VC\\\\Runtimes\\\\x64] 1700000000\n"
        '"Installed"=dword:00000001\n"Version"="v14.44.35211.00"\n\n[Other] 1\n')
    version = installed_vc_runtime(tmp_path)
    assert version == "14.44.35211.00"
    assert version_at_least(version, VC_REDIST_VERSION)
    assert not version_at_least("14.38.33135.0", VC_REDIST_VERSION)
    assert vc_runtime_problem(tmp_path) is None
    assert vc_runtime_problem(tmp_path / "nowhere") is not None


def test_rebase_moves_links_to_the_current_release(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    old, new = releases / "old", releases / "new"
    for rel in (old, new):
        (rel / "files/lib/wine/x86_64-windows").mkdir(parents=True)
        (rel / "files/lib/wine/x86_64-windows/kernel32.dll").write_bytes(rel.name.encode())
    sys32 = tmp_path / "pfx/drive_c/windows/system32"
    sys32.mkdir(parents=True)
    link = sys32 / "kernel32.dll"
    link.symlink_to(old / "files/lib/wine/x86_64-windows/kernel32.dll")
    assert prefix_mod.rebase_runtime_symlinks(tmp_path / "pfx", new) == 1
    assert link.read_bytes() == b"new"


def test_component_checksum_mismatch_refuses(tmp_path: Path) -> None:
    lib = tmp_path / "files/lib/wine/x86_64-windows"
    lib.mkdir(parents=True)
    (lib / "libnghttp2.dll").write_bytes(b"dll")
    (tmp_path / "pfx/drive_c/windows/system32").mkdir(parents=True)
    runtime = Runtime(tmp_path, ["wine-winhttp-http2-v1"], {"libnghttp2.dll": "0" * 64})
    with pytest.raises(XboxPcLaunchError):
        prefix_mod.install_runtime_components(runtime, tmp_path / "pfx")
    runtime.components["libnghttp2.dll"] = hashlib.sha256(b"dll").hexdigest()
    prefix_mod.install_runtime_components(runtime, tmp_path / "pfx")
    assert (tmp_path / "pfx/drive_c/windows/system32/libnghttp2.dll").read_bytes() == b"dll"


def test_eac_runtime_found_only_when_complete(tmp_path: Path) -> None:
    lib64 = tmp_path / "lib1/steamapps/common/Proton EasyAntiCheat Runtime/v2/lib64"
    lib64.mkdir(parents=True)
    (lib64 / "easyanticheat_x64.dll").write_bytes(b"")
    assert installed_eac_runtime([tmp_path / "lib1"]) is None
    (lib64 / "easyanticheat_x64.so").write_bytes(b"")
    assert installed_eac_runtime([tmp_path / "lib0", tmp_path / "lib1"]) == lib64.parent.parent


# ── prefix creation with a fake proton ───────────────────────────────

def _fake_runtime(root: Path, *, creates_prefix: bool) -> Runtime:
    """A runtime whose ``proton`` records its env and maybe fills the prefix."""
    root.mkdir(parents=True)
    body = [
        f"#!{sys.executable}",
        "import json, os, pathlib",
        "data = pathlib.Path(os.environ['STEAM_COMPAT_DATA_PATH'])",
        "(data / 'proton-env.json').write_text(json.dumps(dict(os.environ)))",
    ]
    if creates_prefix:
        body += [
            "s32 = data / 'pfx/drive_c/windows/system32'",
            "s32.mkdir(parents=True, exist_ok=True)",
            "[(s32 / n).write_bytes(b'') for n in ('libvkd3d-utils-1.dll', 'dxgi.dll', 'd3d12.dll')]",
        ]
    else:
        body.append("print('proton went wrong')")
    proton = root / "proton"
    proton.write_text("\n".join(body) + "\n")
    proton.chmod(0o755)
    return Runtime(root)


@pytest.mark.asyncio
async def test_prepare_prefix_creates_with_zero_ids_and_steam_root(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path / "runtime", creates_prefix=True)
    compat = tmp_path / "prefixes" / "9PK087LNGJC5"
    steam_root = tmp_path / "steam"
    notices: list[str] = []

    async def on_create() -> None:
        notices.append("creating")

    base = {"HOME": str(tmp_path), "SteamGameId": "12345", "LD_PRELOAD": "x.so",
            "UNIFIDECK_MICROSOFT_ACTION": "auth", "PATH": os.environ["PATH"]}
    created = await prefix_mod.prepare_prefix(
        runtime, compat, steam_root, base_env=base, on_create=on_create,
    )
    assert created and notices == ["creating"]
    assert (compat / "pfx").is_symlink() and os.readlink(compat / "pfx") == "."
    env = json.loads((compat / "proton-env.json").read_text())
    assert env["SteamGameId"] == "0" and env["SteamAppId"] == "0"
    assert env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(steam_root)
    assert "LD_PRELOAD" not in env and "UNIFIDECK_MICROSOFT_ACTION" not in env
    assert not steam_root.exists(), "the Steam root is never created"
    # A ready prefix is not created twice.
    assert not await prefix_mod.prepare_prefix(runtime, compat, steam_root, base_env=base)


@pytest.mark.asyncio
async def test_prepare_prefix_failure_is_prefix_failed(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path / "runtime", creates_prefix=False)
    with pytest.raises(XboxPcLaunchError) as err:
        await prefix_mod.prepare_prefix(
            runtime, tmp_path / "pfxroot", tmp_path / "steam", base_env={"PATH": os.environ["PATH"]},
        )
    assert err.value.reason == "prefix_failed"
    assert "proton went wrong" in err.value.detail
