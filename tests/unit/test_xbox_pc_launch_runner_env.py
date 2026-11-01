"""Runner script and game env (ported from the launch spike).

``RunnerTests`` and ``EnvTests`` of ``xbox-pc-spike/launch/test_launch.py``,
plus the env order the launcher adds: the Steam identity only with a known
app id, and the user's own options last.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from unifideck.launcher.xbox_pc.environment import (
    build_game_env,
    clean_env,
    locale_env,
    pick_locale,
)
from unifideck.launcher.xbox_pc.output import redact
from unifideck.launcher.xbox_pc.runner import (
    NO_WAIT_FLAG,
    STOP_FLAG,
    nvidia_driver_loaded,
    write_runner,
)
from unifideck.launcher.xbox_pc.runtime import Runtime

_PATH = {"PATH": "/usr/bin:/bin"}


def _fake_wine(root: Path, calls: Path) -> None:
    """Fake wine and wineserver that record their arguments and env."""
    bin_dir = root / "files/bin"
    bin_dir.mkdir(parents=True)
    for name in ("wine", "wineserver"):
        script = bin_dir / name
        script.write_text(
            f'#!/bin/sh\necho "{name} $* prefix=$WINEPREFIX overrides=$WINEDLLOVERRIDES'
            f' eac=$PROTON_EAC_RUNTIME battleye=$PROTON_BATTLEYE_RUNTIME" >> {calls}\n',
        )
        script.chmod(0o755)


def test_runner_sets_prefix_and_waits_for_wineserver(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    _fake_wine(root, calls)
    compat = tmp_path / "prefix"
    compat.mkdir()
    runtime = Runtime(root, ["wine-winhttp-http2-v1"])
    runner = write_runner(runtime, compat)
    assert oct(runner.stat().st_mode & 0o777) == "0o700"
    subprocess.run([str(runner), "/games/x/Game.exe"], check=True, env=_PATH)
    lines = calls.read_text().splitlines()
    assert lines[0].startswith(f"wine /games/x/Game.exe prefix={compat}")
    assert "winhttp=b;libnghttp2=n" in lines[0]
    assert lines[1].startswith("wineserver -w")
    calls.unlink()
    subprocess.run([str(runner), STOP_FLAG], check=True, env=_PATH)
    assert calls.read_text().startswith("wineserver -k")
    calls.unlink()
    # Helper commands in a running prefix must not wait for the game's wineserver.
    subprocess.run([str(runner), NO_WAIT_FLAG, "taskkill", "/IM", "love.exe"], check=True, env=_PATH)
    lines = calls.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"wine taskkill /IM love.exe prefix={compat}")


def test_runner_returns_the_game_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    (root / "files/bin").mkdir(parents=True)
    (root / "files/bin/wine").write_text("#!/bin/sh\nexit 3\n")
    (root / "files/bin/wineserver").write_text("#!/bin/sh\nexit 0\n")
    for name in ("wine", "wineserver"):
        (root / "files/bin" / name).chmod(0o755)
    runner = write_runner(Runtime(root), tmp_path)
    assert subprocess.run([str(runner), "x.exe"], check=False).returncode == 3


def test_runner_exports_the_eac_runtime_unless_the_user_set_one(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    _fake_wine(root, calls)
    eac = tmp_path / "eac rt's"
    runner = write_runner(Runtime(root), tmp_path, eac_runtime=eac)
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True, env=_PATH)
    assert f"eac={eac}" in calls.read_text()
    calls.unlink()
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True,
                   env={**_PATH, "PROTON_EAC_RUNTIME": "/mine"})
    assert "eac=/mine" in calls.read_text()


def test_runner_exports_the_battleye_runtime_unless_the_user_set_one(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    _fake_wine(root, calls)
    battleye = tmp_path / "be rt's"
    runner = write_runner(Runtime(root), tmp_path, battleye_runtime=battleye)
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True, env=_PATH)
    text = calls.read_text()
    assert f"battleye={battleye}" in text and "eac= " in text
    calls.unlink()
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True,
                   env={**_PATH, "PROTON_BATTLEYE_RUNTIME": "/mine"})
    assert "battleye=/mine" in calls.read_text()


def test_runner_keeps_the_users_dll_overrides_and_library_path(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    _fake_wine(root, calls)
    (root / "files/bin/wine").write_text(
        f'#!/bin/sh\necho "overrides=$WINEDLLOVERRIDES ld=$LD_LIBRARY_PATH" >> {calls}\n',
    )
    runner = write_runner(Runtime(root), tmp_path, nvapi=True)
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True, env=_PATH)
    line = calls.read_text()
    assert "overrides=dxgi=n;d3d11=n;d3d10core=n;d3d9=n;d3d12=n;d3d12core=n ld=" in line
    assert ";;" not in line and "ld=:" not in line
    calls.unlink()
    user = {"WINEDLLOVERRIDES": "dinput8=n,b", "LD_LIBRARY_PATH": "/mods/lib"}
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True, env={**_PATH, **user})
    line = calls.read_text()
    assert "d3d12core=n;dinput8=n,b ld=" in line, "the user's overrides come last, so they win"
    assert f"ld=/mods/lib:{root / 'files/lib'}" in line


def test_clean_env_drops_launch_markers() -> None:
    env = clean_env({"EOS_USE_ANTICHEATCLIENTNULL": "1", "APPIMAGE": "x", "LD_LIBRARY_PATH": "/bad",
                     "LD_PRELOAD": "x.so", "UNIFIDECK_MICROSOFT_ACTION": "auth",
                     "HOME": "/home/deck"})
    assert env == {"HOME": "/home/deck"}


def test_locale() -> None:
    assert pick_locale("fr-CA", ["en-US", "fr-FR"]) == "fr-FR"
    assert pick_locale(None, ["en-US"]) is None
    # Not declared: no error, the caller shows a notice and the game picks.
    assert pick_locale("ja-JP", ["en-US"]) is None
    env = locale_env({}, "fr-FR")
    assert env["LANG"] == "fr_FR.UTF-8"
    assert env["XODUS_PACKAGE_LOCALE"] == "fr-FR"


def test_redact() -> None:
    token = "eyJ" + "a" * 30 + "." + "b" * 40 + "." + "c" * 40
    assert redact(f"auth {token} done") == "auth [redacted] done"
    line = "created a temporary launch view at /home/deck/.xodus-chain-1"
    assert redact(line) == line


def test_unknown_app_id_keeps_the_inherited_steam_game_id(tmp_path: Path) -> None:
    base = {"SteamGameId": "3123456789", "HOME": "/h", "WINEDEBUG": "+relay"}
    env = build_game_env(base, credential_dir=tmp_path, window_env=None)
    assert env["SteamGameId"] == "3123456789"
    assert env["WINEDEBUG"] == "+relay", "a user's WINEDEBUG is kept"
    assert env["XODUS_CREDENTIAL_DIR"] == str(tmp_path)


def test_known_app_id_overrides_and_user_options_win(tmp_path: Path) -> None:
    base = {"SteamGameId": "0", "HOME": "/h"}
    env = build_game_env(
        base, credential_dir=tmp_path, locale="en-US",
        window_env={"SteamGameId": "42", "SteamAppId": "42"},
        service_env={"XODUS_SOCKET": "/run/x.sock"},
        overrides={"XODUS_LOG": "debug", "SteamAppId": "7"},
    )
    assert env["SteamGameId"] == "42"
    assert env["SteamAppId"] == "7"
    assert env["XODUS_LOG"] == "debug"
    assert env["XODUS_SOCKET"] == "/run/x.sock"
    assert env["WINEDEBUG"] == "fixme-all"
    assert env["VKD3D_DEBUG"] == env["VKD3D_SHADER_DEBUG"] == "err", (
        "at warn vkd3d wrote several lines per shader while the game compiled"
    )
    assert env["WINEUSERLOCALE"] == "en-US"


def test_runner_runs_a_32_bit_game_in_new_wow64(tmp_path: Path) -> None:
    # I-038: under win64 a 32-bit game gets a 32-bit Unix side, which ran
    # out of address space and clamped the Steam app id to 2147483647.
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    _fake_wine(root, calls)
    (root / "files/bin/wine").write_text(f'#!/bin/sh\necho "arch=$WINEARCH" >> {calls}\n')
    subprocess.run([str(write_runner(Runtime(root), tmp_path)), NO_WAIT_FLAG, "x"], check=True, env=_PATH)
    assert calls.read_text() == "arch=win64\n"
    calls.unlink()
    runner = write_runner(Runtime(root), tmp_path, wow64=True)
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True, env=_PATH)
    assert calls.read_text() == "arch=wow64\n"


def test_runner_disables_nvapi_without_an_nvidia_driver(tmp_path: Path) -> None:
    # I-035: with no NVIDIA GPU each NVAPI call opens a Vulkan instance.
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    _fake_wine(root, calls)
    runner = write_runner(Runtime(root), tmp_path, nvapi=False)
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True,
                   env={**_PATH, "WINEDLLOVERRIDES": "nvapi=n"})
    assert "d3d12core=n;nvapi=d;nvapi64=d;nvofapi64=d;nvapi=n eac=" in calls.read_text(), (
        "ours disable NVAPI and the user's own override comes last, so it wins"
    )
    calls.unlink()
    subprocess.run([str(write_runner(Runtime(root), tmp_path, nvapi=True)), NO_WAIT_FLAG, "x"],
                   check=True, env=_PATH)
    assert "nvapi=d" not in calls.read_text()


def test_nvidia_driver_loaded(tmp_path: Path) -> None:
    modules = tmp_path / "modules"
    modules.write_text("amdgpu 16384 0 - Live 0x0\nsnd 8192 1 amdgpu, Live 0x0\n")
    assert not nvidia_driver_loaded(modules)
    modules.write_text("nvidia_drm 8192 0 - Live 0x0\nnvidia 16384 1 nvidia_drm, Live 0x0\n")
    assert nvidia_driver_loaded(modules)
    assert nvidia_driver_loaded(tmp_path / "missing"), "unknown keeps NVAPI, Proton's default"


def test_runner_keeps_the_vkd3d_shader_cache_in_the_prefix(tmp_path: Path) -> None:
    # Without it vkd3d-proton wrote its cache into the temporary launch
    # view, which is deleted when the game ends: every launch started cold.
    root = tmp_path / "runtime"
    calls = tmp_path / "calls"
    _fake_wine(root, calls)
    (root / "files/bin/wine").write_text(f'#!/bin/sh\necho "cache=$VKD3D_SHADER_CACHE_PATH" >> {calls}\n')
    compat = tmp_path / "prefix"
    compat.mkdir()
    runner = write_runner(Runtime(root), compat)
    assert (compat / "vkd3d-proton").is_dir()
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True, env=_PATH)
    windows = "Z:" + str(compat / "vkd3d-proton").replace("/", "\\")
    assert calls.read_text() == f"cache={windows}\n"
    calls.unlink()
    subprocess.run([str(runner), NO_WAIT_FLAG, "x"], check=True,
                   env={**_PATH, "VKD3D_SHADER_CACHE_PATH": "Z:\\mine"})
    assert calls.read_text() == "cache=Z:\\mine\n", "a user's cache path wins"


def test_a_known_title_gets_its_runtime_feature_and_the_user_can_turn_it_off(tmp_path: Path) -> None:
    from unifideck.launcher.xbox_pc.title_env import title_env

    extra = title_env("c261457lcnmj")
    assert extra == {"XODUS_GAMEINPUT_V3": "1"}
    env = build_game_env({"HOME": "/h"}, credential_dir=tmp_path, title_env=extra)
    assert env["XODUS_GAMEINPUT_V3"] == "1"
    env = build_game_env(
        {"HOME": "/h"}, credential_dir=tmp_path, title_env=extra, overrides={"XODUS_GAMEINPUT_V3": "0"},
    )
    assert env["XODUS_GAMEINPUT_V3"] == "0", "the user's launch option wins"


def test_any_other_title_gets_no_runtime_feature() -> None:
    from unifideck.launcher.xbox_pc.title_env import TITLE_ENV, title_env

    assert title_env("9PK087LNGJC5") == {}
    assert title_env(None) == {}
    title_env("C261457LCNMJ")["XODUS_GAMEINPUT_V3"] = "changed"
    assert TITLE_ENV["C261457LCNMJ"]["XODUS_GAMEINPUT_V3"] == "1", "callers get a copy"
