"""A whole Xbox PC launch against a fake world: preflight, locks, env, stop.

Everything Xodus would run is a small script: ``xodus-cli`` records its
argv and env, ``xodus-service`` binds its socket, ``proton`` fills the
prefix, and ``wine``/``wineserver`` do nothing. The components are
installed the way the plugin installs them (``active.json`` plus a
manifest), so the real checks run. ``HOME`` is a temporary folder (root
conftest) and ``XDG_RUNTIME_DIR`` a short one.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.proton.infrastructure.prefix_layout import game_prefix_root
from unifideck.launcher.types.context import LaunchContext
from unifideck.launcher.xbox_pc import component_locks
from unifideck.launcher.xbox_pc import session as session_mod
from unifideck.launcher.xbox_pc.argv import run_argv
from unifideck.launcher.xbox_pc.errors import LANGUAGE_UNAVAILABLE_KEY, XboxPcLaunchError
from unifideck.launcher.xbox_pc.install_records import write_install_record
from unifideck.launcher.xbox_pc.layout import (
    ActiveComponents,
    backend_root,
    credential_dir,
    data_root,
    ensure_private_dir,
    install_lock_path,
    runtime_root,
    write_active,
)
from unifideck.launcher.xbox_pc.stop import StagedStopper

PID = "9PK087LNGJC5"
FEATURES = ["cloud-saves-pr28", "xodus-tcp-bridge-v1"]
CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Game configVersion="1">
  <StoreId>9PK087LNGJC5</StoreId>
  <ExecutableList><Executable Name="Balatro.exe" Id="Game"/></ExecutableList>
  <ShellVisuals DefaultDisplayName="Balatro"/>
  <Resources><Resource Language="en-US"/></Resources>
</Game>
"""
_CLI = """
import json, os, signal, sys, time
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
with open(os.environ["FAKE_OUT"], "w") as fh:
    json.dump({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd()}, fh)
print(os.environ.get("FAKE_PRINT", "game running"), flush=True)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""
_SERVICE = """
import os, signal, socket, sys
path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "xodus.sock")
s = socket.socket(socket.AF_UNIX); s.bind(path); s.listen(4)
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
while True:
    s.accept()[0].close()
"""
_PROTON = """
import os, pathlib
s32 = pathlib.Path(os.environ["STEAM_COMPAT_DATA_PATH"]) / "pfx/drive_c/windows/system32"
s32.mkdir(parents=True, exist_ok=True)
for n in ("libvkd3d-utils-1.dll", "dxgi.dll", "d3d12.dll"):
    (s32 / n).write_bytes(b"")
"""


def _script(path: Path, body: str, *, python: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((f"#!{sys.executable}\n" if python else "#!/bin/sh\n") + body)
    path.chmod(0o755)


def _install_components(features: list[str]) -> None:
    root = ensure_private_dir(data_root())
    backend = backend_root() / "0.1.0-abcd1234"
    _script(backend / "xodus-cli", _CLI)
    _script(backend / "xodus-service", _SERVICE)
    (backend / "LICENSE").write_text("MIT")
    files = {n: hashlib.sha256((backend / n).read_bytes()).hexdigest()
             for n in ("xodus-cli", "xodus-service", "LICENSE")}
    (backend / "manifest.json").write_text(json.dumps({"files": files}))
    runtime = runtime_root() / "0.1.0"
    _script(runtime / "proton", _PROTON)
    _script(runtime / "files/bin/wine", "exit 0\n", python=False)
    _script(runtime / "files/bin/wineserver", "exit 0\n", python=False)
    (runtime / "files/share/default_pfx").mkdir(parents=True)
    (runtime / "XODUS_NEXUS_PATCH.json").write_text(json.dumps({"features": features}))
    assert root.is_dir()
    write_active(ActiveComponents(
        backend_version="0.1.0", backend_dir=backend,
        manifest_sha256=hashlib.sha256((backend / "manifest.json").read_bytes()).hexdigest(),
        runtime_version="0.1.0", runtime_dir=runtime, archive_sha512="0" * 128,
        features=tuple(features),
    ))


def _install_game(
    base: Path, *, state: str = "installed", market: str | None = "IN", store_game_id: str | None = None,
) -> Path:
    install_root = base / "Xbox" / PID
    game = install_root / "game"
    game.mkdir(parents=True)
    (game / "MicrosoftGame.config").write_text(CONFIG)
    (game / ".xodus-streaming.msixvc").write_bytes(b"")
    (install_root / ".xodus-chain-stale").mkdir()
    record: dict[str, Any] = {
        "schema": 1, "productId": PID, "state": state, "installRoot": str(install_root),
        "gameDir": str(game), "exe": str(game / "Balatro.exe"), "exeRelative": "Balatro.exe",
    }
    if market:
        record["market"] = market
    if store_game_id:
        record["storeGameId"] = store_game_id
    write_install_record(record)
    return game


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    run_dir = Path(tempfile.mkdtemp(prefix="xpcl"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    _install_components(FEATURES)
    ensure_private_dir(credential_dir())
    (credential_dir() / "user-session").write_text("{}")
    game = _install_game(tmp_path / "library")
    out = tmp_path / "cli.json"
    base = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "SteamGameId": "777",
            "FAKE_OUT": str(out), "LD_PRELOAD": "evil.so"}
    yield {"game": game, "out": out, "base": base, "steam_root": tmp_path / "steam", "run_dir": run_dir}
    shutil.rmtree(run_dir, ignore_errors=True)


def _ctx(**overrides: Any) -> LaunchContext:
    fields: dict[str, Any] = {
        "store": "microsoft", "game_id": PID, "exe_path": Path("/x/Balatro.exe"),
        "work_dir": Path("/x"), "plugin_dir": Path("/p"), "launch_backend": "xodus",
    }
    fields.update(overrides)
    return LaunchContext(**fields)


def _deps(world: dict[str, Any], **overrides: Any) -> session_mod.LaunchDeps:
    fields: dict[str, Any] = {
        "steam_root": world["steam_root"], "base_env": world["base"],
        "language": "en-US", "fast_fail_s": 0.0,
    }
    fields.update(overrides)
    return session_mod.LaunchDeps(**fields)


@pytest.mark.asyncio
async def test_full_launch_runs_xodus_cli_with_the_right_argv_and_env(world: dict[str, Any]) -> None:
    notices: list[str] = []
    spawned: list[bool] = []

    async def notify(key: str, _params: Any) -> None:
        notices.append(key)

    async def on_spawned() -> None:
        spawned.append(True)

    rc = await session_mod.XboxPcLaunch(
        _ctx(env_overrides={"MY_VAR": "1"}), _deps(world, notify=notify, on_spawned=on_spawned),
    ).run()
    assert rc == 0 and spawned == [True]
    seen = json.loads(world["out"].read_text())
    prefix = game_prefix_root(PID)
    runner = prefix / "unifideck-xbox-wine"
    cli = backend_root() / "0.1.0-abcd1234" / "xodus-cli"
    assert seen["argv"][1:] == run_argv(cli, world["game"], runner, "Balatro.exe", "IN")[1:]
    assert seen["cwd"] == str(world["game"])
    env = seen["env"]
    assert env["SteamGameId"] == "777", "an unknown app id keeps Steam's SteamGameId"
    assert env["XODUS_CREDENTIAL_DIR"] == str(credential_dir())
    assert env["XODUS_SAVE_SYNC"] == "auto" and len(env["XODUS_BRIDGE_KEY"]) == 64
    assert env["MY_VAR"] == "1" and "LD_PRELOAD" not in env
    assert env["WINEUSERLOCALE"] == "en-US"
    assert "toasts.launcher.initializingPrefix" in notices
    assert not (world["game"].parent / ".xodus-chain-stale").exists()
    assert not world["steam_root"].exists(), "~/.steam is never created"
    assert list((world["run_dir"] / "unifideck-xbox-pc").iterdir()) == []
    with component_locks.game_lock(PID) as held:
        assert held, "the game lock is released after the run"


@pytest.mark.asyncio
async def test_a_known_app_id_sets_the_window_identity(world: dict[str, Any]) -> None:
    window = {"SteamGameId": "4000000001", "SteamAppId": "4000000001"}
    await session_mod.XboxPcLaunch(_ctx(), _deps(world, window_env=window)).run()
    assert json.loads(world["out"].read_text())["env"]["SteamGameId"] == "4000000001"


@pytest.mark.asyncio
async def test_a_fast_failure_is_classified(world: dict[str, Any]) -> None:
    world["base"].update({"FAKE_RC": "1", "FAKE_PRINT": "error: not signed in"})
    with pytest.raises(XboxPcLaunchError) as err:
        await session_mod.XboxPcLaunch(_ctx(), _deps(world, fast_fail_s=60.0)).run()
    assert err.value.reason == "sign_in_required"


@pytest.mark.asyncio
async def test_a_long_run_returns_its_exit_code(world: dict[str, Any]) -> None:
    world["base"]["FAKE_RC"] = "3"
    assert await session_mod.XboxPcLaunch(_ctx(), _deps(world)).run() == 3


def _runner_arch() -> str:
    text = (game_prefix_root(PID) / "unifideck-xbox-wine").read_text()
    return next(line for line in text.splitlines() if line.startswith("export WINEARCH="))


@pytest.mark.asyncio
async def test_a_32_bit_game_runs_in_new_wow64(world: dict[str, Any]) -> None:
    await session_mod.XboxPcLaunch(_ctx(), _deps(world)).run()
    assert _runner_arch() == "export WINEARCH='win64'"
    config = world["game"] / "MicrosoftGame.config"
    config.write_text(CONFIG.replace(
        "</Game>",
        "<DesktopRegistration><ProcessorArchitecture>x86</ProcessorArchitecture></DesktopRegistration></Game>",
    ))
    await session_mod.XboxPcLaunch(_ctx(), _deps(world)).run()
    assert _runner_arch() == "export WINEARCH='wow64'"


_WINE_CRASH = "wine: Unhandled exception 0xc0000417 in thread f4, starting debugger..."


@pytest.mark.asyncio
async def test_a_crash_soon_after_the_start_is_reported(world: dict[str, Any]) -> None:
    # I-035: xodus-cli exits 0 after the game crashed.
    world["base"]["FAKE_PRINT"] = _WINE_CRASH
    with pytest.raises(XboxPcLaunchError) as err:
        await session_mod.XboxPcLaunch(_ctx(), _deps(world)).run()
    assert err.value.reason == "game_crashed"


@pytest.mark.asyncio
async def test_a_crash_after_a_real_session_returns_its_exit_code(world: dict[str, Any]) -> None:
    world["base"]["FAKE_PRINT"] = _WINE_CRASH
    assert await session_mod.XboxPcLaunch(_ctx(), _deps(world, crash_window_s=0.0)).run() == 0


@pytest.mark.asyncio
async def test_an_undeclared_language_is_a_notice_not_a_refusal(world: dict[str, Any]) -> None:
    notices: list[tuple[str, Any]] = []

    async def notify(key: str, params: Any) -> None:
        notices.append((key, dict(params)))

    rc = await session_mod.XboxPcLaunch(_ctx(), _deps(world, language="ja-JP", notify=notify)).run()
    assert rc == 0
    assert (LANGUAGE_UNAVAILABLE_KEY, {"language": "ja-JP"}) in notices


@pytest.mark.asyncio
async def test_a_second_launch_of_the_same_game_refuses(world: dict[str, Any]) -> None:
    with component_locks.game_lock(PID) as held:
        assert held
        with pytest.raises(XboxPcLaunchError) as err:
            await session_mod.XboxPcLaunch(_ctx(), _deps(world)).run()
    assert err.value.reason == "already_running"


@pytest.mark.asyncio
async def test_the_runtime_is_held_shared_for_the_whole_session(world: dict[str, Any]) -> None:
    world["base"]["FAKE_SLEEP"] = "1.5"
    task = asyncio.ensure_future(session_mod.XboxPcLaunch(_ctx(), _deps(world)).run())
    for _ in range(100):
        if world["out"].exists():
            break
        await asyncio.sleep(0.05)
    lock = component_locks.runtime_lock_path("0.1.0")
    assert not component_locks.try_exclusive(lock), "GC cannot take the runtime mid-game"
    assert component_locks.components_in_use()
    assert await task == 0
    assert component_locks.try_exclusive(lock)


@pytest.mark.asyncio
async def test_cancel_runs_the_staged_stop_and_cleans_up(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_mod, "StagedStopper",
                        functools.partial(StagedStopper, close_grace_s=0.2, sigint_grace_s=5))
    world["base"]["FAKE_SLEEP"] = "60"
    task = asyncio.ensure_future(session_mod.XboxPcLaunch(_ctx(), _deps(world)).run())
    for _ in range(100):
        if world["out"].exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=20)
    assert task in done and task.cancelled()
    assert list((world["run_dir"] / "unifideck-xbox-pc").iterdir()) == []
    with component_locks.game_lock(PID) as held:
        assert held


@pytest.mark.asyncio
async def test_a_stop_during_teardown_still_stops_the_service(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Steam re-sends SIGTERM. A cancel inside service.stop() must not orphan it."""
    monkeypatch.setattr(session_mod, "StagedStopper",
                        functools.partial(StagedStopper, close_grace_s=0.2, sigint_grace_s=5))
    stopping = asyncio.Event()
    services: list[Any] = []
    real_stop = session_mod.ServiceSession.stop

    async def slow_stop(self: Any) -> None:
        services.append(self.proc)
        stopping.set()
        await asyncio.sleep(0.3)
        await real_stop(self)

    monkeypatch.setattr(session_mod.ServiceSession, "stop", slow_stop)
    world["base"]["FAKE_SLEEP"] = "60"
    task = asyncio.ensure_future(session_mod.XboxPcLaunch(_ctx(), _deps(world)).run())
    for _ in range(100):
        if world["out"].exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 20)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=20)
    assert task in done and task.cancelled()
    assert services and services[0].returncode is not None, "xodus-service was reaped"
    assert list((world["run_dir"] / "unifideck-xbox-pc").iterdir()) == []
    with component_locks.game_lock(PID) as held:
        assert held


@pytest.mark.asyncio
async def test_a_fast_failure_after_a_slow_prefix_is_still_classified(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fast-fail window starts when xodus-cli starts, not before the prefix."""
    real_prepare = session_mod.prepare_prefix

    async def slow_prepare(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(1.0)
        await real_prepare(*args, **kwargs)

    monkeypatch.setattr(session_mod, "prepare_prefix", slow_prepare)
    world["base"].update({"FAKE_RC": "1", "FAKE_PRINT": "error: not signed in"})
    with pytest.raises(XboxPcLaunchError) as err:
        await session_mod.XboxPcLaunch(_ctx(), _deps(world, fast_fail_s=0.9)).run()
    assert err.value.reason == "sign_in_required"


@pytest.mark.asyncio
async def test_locks_taken_after_a_cancel_are_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock wait runs in a thread. A cancel meanwhile must not leak its locks."""
    import contextlib
    import threading
    import time

    entered = threading.Event()
    released = threading.Event()

    @contextlib.contextmanager
    def slow_components() -> Iterator[None]:
        entered.set()
        time.sleep(0.3)
        try:
            yield None
        finally:
            released.set()

    monkeypatch.setattr(session_mod, "reading_components", slow_components)
    with contextlib.ExitStack() as stack:
        task = asyncio.ensure_future(session_mod._hold_components(stack))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        assert task in done and task.cancelled()
    assert await asyncio.to_thread(released.wait, 5), "the late locks were released"


@pytest.mark.asyncio
async def test_the_record_is_found_from_a_row_id_that_is_not_the_product_id(
    world: dict[str, Any], tmp_path: Path,
) -> None:
    """A merged row launches under its xCloud id; the record keeps both ids."""
    shutil.rmtree(tmp_path / "library")
    _install_game(tmp_path / "library", store_game_id="9NXCLOUDID01")
    assert await session_mod.XboxPcLaunch(_ctx(game_id="9NXCLOUDID01"), _deps(world)).run() == 0
    assert (game_prefix_root(PID) / "unifideck-xbox-wine").is_file(), "the prefix is named after productId"
    assert await session_mod.XboxPcLaunch(_ctx(game_id=PID.lower()), _deps(world)).run() == 0


@pytest.mark.asyncio
async def test_an_unknown_row_id_is_not_downloaded(world: dict[str, Any]) -> None:
    with pytest.raises(XboxPcLaunchError) as err:
        await session_mod.XboxPcLaunch(_ctx(game_id="9NOTINSTALL1"), _deps(world)).run()
    assert err.value.reason == "not_downloaded"


# ── refusals ─────────────────────────────────────────────────────────

async def _reason(world: dict[str, Any], **deps: Any) -> str:
    with pytest.raises(XboxPcLaunchError) as err:
        await session_mod.XboxPcLaunch(_ctx(), _deps(world, **deps)).run()
    assert not world["out"].exists(), "xodus-cli never ran"
    return err.value.reason


@pytest.mark.asyncio
async def test_refuses_without_a_steam_root(world: dict[str, Any]) -> None:
    assert await _reason(world, steam_root=None) == "steam_root_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "market", "reason"), [
    ("updating", "IN", "not_downloaded"),
    ("partial", "IN", "not_downloaded"),
    ("installed", None, "market_unknown"),
    ("installed", "neutral", "market_unknown"),
])
async def test_refuses_an_incomplete_record(
    world: dict[str, Any], tmp_path: Path, state: str, market: str | None, reason: str,
) -> None:
    shutil.rmtree(tmp_path / "library")
    _install_game(tmp_path / "library", state=state, market=market)
    assert await _reason(world) == reason


@pytest.mark.asyncio
async def test_refuses_when_signed_out(world: dict[str, Any]) -> None:
    (credential_dir() / "user-session").unlink()
    assert await _reason(world) == "sign_in_required"


@pytest.mark.asyncio
async def test_refuses_without_components(world: dict[str, Any]) -> None:
    (data_root() / "active.json").unlink()
    assert await _reason(world) == "runtime_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize(("features", "reason"), [
    ([], "cloud_save_unavailable"),
    (["cloud-saves-pr28"], "runtime_outdated"),
])
async def test_refuses_a_runtime_without_cloud_saves(
    world: dict[str, Any], features: list[str], reason: str,
) -> None:
    meta = runtime_root() / "0.1.0" / "XODUS_NEXUS_PATCH.json"
    meta.write_text(json.dumps({"features": features}))
    assert await _reason(world) == reason


@pytest.mark.asyncio
async def test_refuses_a_tampered_backend(world: dict[str, Any]) -> None:
    (backend_root() / "0.1.0-abcd1234" / "xodus-cli").write_text("#!/bin/sh\nexit 0\n")
    assert await _reason(world) == "backend_tampered"


@pytest.mark.asyncio
async def test_refuses_while_components_are_promoting(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unifideck.launcher.proton.infrastructure.file_lock import flock_path

    monkeypatch.setattr(session_mod, "reading_components",
                        lambda: component_locks.reading_components(timeout_s=0.2))
    with flock_path(install_lock_path()) as held:
        assert held
        assert await _reason(world) == "components_busy"


@pytest.mark.asyncio
async def test_refuses_an_eac_game_without_steams_eac_runtime(world: dict[str, Any]) -> None:
    (world["game"] / "EasyAntiCheat").mkdir()
    (world["game"] / "EasyAntiCheat" / "Settings.json").write_text("{}")
    assert await _reason(world) == "eac_runtime_missing"


@pytest.mark.asyncio
async def test_refuses_an_encrypted_vc_redist_without_vc_in_the_prefix(world: dict[str, Any]) -> None:
    config = world["game"] / "MicrosoftGame.config"
    config.write_text(CONFIG.replace(
        "</Game>", '<KnownDependencyList><KnownDependency Name="VC14"/></KnownDependencyList></Game>'))
    redist = world["game"] / "Engine/Extras/Redist/en-us"
    redist.mkdir(parents=True)
    (redist / "vc_redist.x64.exe").write_bytes(b"\x00enc")
    assert await _reason(world) == "vcredist_missing"


@pytest.mark.asyncio
async def test_refuses_a_missing_container(world: dict[str, Any]) -> None:
    (world["game"] / ".xodus-streaming.msixvc").unlink()
    assert await _reason(world) == "not_downloaded"
