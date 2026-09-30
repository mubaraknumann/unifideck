"""Tests for the "Frame Generation (OptiScaler)" feature (OptiScalerRPCMixin).

Regression context: Decky-Framegen's ``~/fgmod/fgmod %command%`` Launch
Options workflow never worked for a Unifideck-managed shortcut — its
``Exe`` always points at ``bin/unifideck-launcher``, never the game's own
binary, so fgmod's ``*.exe`` argv-sniffing (see ``fgmod.sh``) never finds a
match and its ``STEAM_COMPAT_INSTALL_PATH`` fallback isn't populated either
(Steam launches the launcher script directly, not through Proton). DLSS/FSR
files ended up copied next to the launcher script, or the plugin's own
game auto-detection missed Unifideck games entirely.

This mixin instead drives the SAME ``~/fgmod/fgmod`` wrapper Decky-Framegen
installs, but with the install dir it already resolves correctly (games.map
``work_dir``, the ``ExecutableRPCMixin`` helper) passed directly as fgmod's
one argument — the "standalone" invocation path fgmod.sh supports, bypassing
the broken argv-sniffing entirely.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from unifideck.rpc import RpcError
from unifideck.rpc.mixins.optiscaler import OptiScalerRPCMixin


class _FakeConfig:
    def __init__(self):
        self.d: dict = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def _make_host(install_dir: str = "") -> OptiScalerRPCMixin:
    host = OptiScalerRPCMixin()
    host.config = _FakeConfig()
    if install_dir:
        host._install_dir = lambda store, game_id: install_dir  # type: ignore[method-assign]
    else:
        host._install_dir = lambda store, game_id: ""  # type: ignore[method-assign]
    return host


class _FakeEntry:
    def __init__(self, exe: str):
        self.exe = exe


class _FakeShortcutService:
    def __init__(self, exe: str | None):
        self._exe = exe

    async def get_entry_for_game_key(self, store, game_id):
        return _FakeEntry(self._exe) if self._exe else None


def _with_games_map_exe(host: OptiScalerRPCMixin, exe: str | None) -> None:
    """Wire up ``host.services.shortcut`` so ``_patch_target_dir`` resolves
    the exe's OWN directory instead of falling back to ``_install_dir``."""
    host.services = type(  # type: ignore[attr-defined]
        "S", (), {"shortcut": _FakeShortcutService(exe)},
    )()


# ── _patch_target_dir (nested install-folder regression) ─────────────────
def test_patch_target_dir_uses_exe_own_folder_not_install_root(tmp_path):
    """Regression: some installs extract into a nested subfolder repeating
    the title (Ghost.of.Tsushima/Ghost.of.Tsushima/…). fgmod must patch the
    EXE's own directory — Windows only searches there for DLLs — not the
    outer work_dir install root, or DLSS-Enabler's DLLs land one level too
    high and the game runs unpatched despite fgmod reporting success."""
    root = tmp_path / "Ghost.of.Tsushima"
    nested = root / "Ghost.of.Tsushima"
    nested.mkdir(parents=True)
    exe = nested / "Ghost.exe"
    exe.write_text("")

    host = _make_host(str(root))
    _with_games_map_exe(host, str(exe))

    target = asyncio.run(host._patch_target_dir("gog", "123"))

    assert target == str(nested)


def test_patch_target_dir_falls_back_to_install_root_without_games_map_entry(
    tmp_path,
):
    host = _make_host(str(tmp_path))
    _with_games_map_exe(host, None)

    target = asyncio.run(host._patch_target_dir("gog", "123"))

    assert target == str(tmp_path)


def test_patch_target_dir_falls_back_when_games_map_exe_missing_on_disk(
    tmp_path,
):
    """A stale games.map row pointing at a since-moved/renamed exe must not
    be trusted — fall back to the install root rather than a dead path."""
    host = _make_host(str(tmp_path))
    _with_games_map_exe(host, str(tmp_path / "no-such.exe"))

    target = asyncio.run(host._patch_target_dir("gog", "123"))

    assert target == str(tmp_path)


def test_patch_target_dir_falls_back_without_services_wired(tmp_path):
    """No ``services`` attribute at all (shouldn't happen once the Plugin
    class is composed, but the resolver must degrade gracefully)."""
    host = _make_host(str(tmp_path))

    target = asyncio.run(host._patch_target_dir("gog", "123"))

    assert target == str(tmp_path)


# ── get_optiscaler_status ───────────────────────────────────────────────
def test_status_reports_fgmod_not_installed_by_default(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", tmp_path / "no-such-fgmod")
    host = _make_host(str(tmp_path))

    status = asyncio.run(host.get_optiscaler_status("gog", "123"))

    assert status["fgmod_installed"] is False
    assert status["install_dir"] == str(tmp_path)
    assert status["patched"] is False


def test_status_reports_fgmod_installed_when_script_present(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)
    host = _make_host(str(tmp_path))

    status = asyncio.run(host.get_optiscaler_status("gog", "123"))

    assert status["fgmod_installed"] is True


def test_status_reports_patched_when_legacy_marker_dll_present(tmp_path, monkeypatch):
    """Older fgmod builds: single ``dlss-enabler.dll`` marker."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", tmp_path / "fgmod")
    (tmp_path / "dlss-enabler.dll").write_text("")
    host = _make_host(str(tmp_path))

    status = asyncio.run(host.get_optiscaler_status("gog", "123"))

    assert status["patched"] is True


def test_status_reports_patched_when_current_fingerprint_present(tmp_path, monkeypatch):
    """Regression: current fgmod builds no longer write dlss-enabler.dll at
    all — they write OptiScaler.ini/fakenvapi.*/D3D12_Optiscaler/etc
    instead. A status check hardcoded to the old marker kept reporting
    "not patched" for every current-fgmod user despite a successful patch
    (fgmod itself logged success)."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", tmp_path / "fgmod")
    (tmp_path / "OptiScaler.ini").write_text("")
    (tmp_path / "fakenvapi.dll").write_text("")
    host = _make_host(str(tmp_path))

    status = asyncio.run(host.get_optiscaler_status("gog", "123"))

    assert status["patched"] is True


def test_status_reports_patched_via_d3d12_optiscaler_dir(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", tmp_path / "fgmod")
    (tmp_path / "D3D12_Optiscaler").mkdir()
    host = _make_host(str(tmp_path))

    status = asyncio.run(host.get_optiscaler_status("gog", "123"))

    assert status["patched"] is True


def test_status_reports_general_env_overrides(tmp_path, monkeypatch):
    """Regression: users couldn't tell whether env vars set via the general
    "Environment variables…" modal actually reach OptiScaler. Status now
    mirrors the same general store read-only so the modal can display it."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", tmp_path / "fgmod")
    host = _make_host(str(tmp_path))
    host.config.set("games.gog:123.env_overrides", {"MANGOHUD": "1"})

    status = asyncio.run(host.get_optiscaler_status("gog", "123"))

    assert status["env"] == {"MANGOHUD": "1"}


def test_status_reports_empty_env_by_default(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", tmp_path / "fgmod")
    host = _make_host(str(tmp_path))

    status = asyncio.run(host.get_optiscaler_status("gog", "123"))

    assert status["env"] == {}


def test_status_rejects_missing_args():
    host = _make_host()
    with pytest.raises(RpcError):
        asyncio.run(host.get_optiscaler_status("", "123"))


# ── apply_optiscaler_patch ───────────────────────────────────────────────
def test_apply_patch_rejects_when_fgmod_not_installed(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", tmp_path / "missing-fgmod")
    host = _make_host(str(tmp_path))

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.apply_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "fgmod_not_installed"


def test_apply_patch_rejects_when_install_dir_unresolved(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)
    host = _make_host("")  # unresolved install dir

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.apply_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "install_dir_unresolved"


def test_apply_patch_invokes_fgmod_with_install_dir_as_sole_arg(tmp_path, monkeypatch):
    """The core regression fix: fgmod gets the RESOLVED install dir directly
    (the "standalone" invocation), never the broken %command%-argv path."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    host = _make_host(str(game_dir))

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"Done!\n", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    res = asyncio.run(host.apply_optiscaler_patch("gog", "123"))

    assert res["success"] is True
    assert captured["argv"] == (str(fgmod), str(game_dir))


def test_apply_patch_targets_nested_exe_folder_when_resolvable(tmp_path, monkeypatch):
    """End-to-end: apply_optiscaler_patch must hand fgmod the EXE's own
    nested folder, not the games.map work_dir install root, when a
    games.map row with a real exe on disk exists."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)

    root = tmp_path / "Ghost.of.Tsushima"
    nested = root / "Ghost.of.Tsushima"
    nested.mkdir(parents=True)
    exe = nested / "Ghost.exe"
    exe.write_text("")

    host = _make_host(str(root))
    _with_games_map_exe(host, str(exe))

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"Done!\n", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    res = asyncio.run(host.apply_optiscaler_patch("gog", "123"))

    assert res["success"] is True
    assert captured["argv"] == (str(fgmod), str(nested))


def test_apply_patch_merges_general_env_overrides(tmp_path, monkeypatch):
    """Regression: OptiScaler no longer has its OWN env-var config — it
    reads the general "Environment variables…" store (GameEnvRPCMixin,
    games.<store>:<game_id>.env_overrides) so there's one place to set env
    vars per game, applied to both the game's own launch and the patch
    step."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    host = _make_host(str(game_dir))
    host.config.set("games.gog:123.env_overrides", {"Dx12Upscaler": "fsr31"})

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    res = asyncio.run(host.apply_optiscaler_patch("gog", "123"))

    assert captured["env"]["Dx12Upscaler"] == "fsr31"
    # The rest of the process's own environment must still be present.
    assert captured["env"].get("PATH") == os.environ.get("PATH")
    # Returned so the modal can confirm which vars were actually applied.
    assert res["env"] == {"Dx12Upscaler": "fsr31"}


def test_apply_patch_sanitizes_frozen_loader_env(tmp_path, monkeypatch):
    """Regression: the Decky plugin process is PyInstaller-frozen, so
    os.environ carries a poisoned LD_LIBRARY_PATH/_ORIG. Passed through
    untouched, it breaks bash itself (rc=127, ``undefined symbol:
    rl_trim_arg_from_keyseq``) when fgmod (a bash script) is invoked — the
    exact failure observed in the field. Must be stripped before use."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")  # noqa: S108
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/pressure-vessel/overrides")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/_MEIxxxx/libfoo.so")  # noqa: S108

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    host = _make_host(str(game_dir))

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    asyncio.run(host.apply_optiscaler_patch("gog", "123"))

    assert "LD_LIBRARY_PATH" not in captured["env"]
    assert "LD_LIBRARY_PATH_ORIG" not in captured["env"]
    assert "LD_PRELOAD" not in captured["env"]


def test_apply_patch_raises_on_nonzero_exit(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    host = _make_host(str(game_dir))

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"No write permission to the game folder!\n", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.apply_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "patch_failed"


def test_apply_patch_kills_process_and_raises_on_timeout(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)
    monkeypatch.setattr(optiscaler_mod, "_PATCH_TIMEOUT_SECONDS", 0.01)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    host = _make_host(str(game_dir))

    killed = {"called": False}

    class _FakeProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed["called"] = True

        async def wait(self):
            return 0

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.apply_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "patch_timed_out"
    assert killed["called"] is True


def test_apply_patch_rejects_missing_args():
    host = _make_host()
    with pytest.raises(RpcError):
        asyncio.run(host.apply_optiscaler_patch("", "123"))


# ── remove_optiscaler_patch ──────────────────────────────────────────────
def test_remove_patch_rejects_when_not_patched(tmp_path):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    host = _make_host(str(game_dir))

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.remove_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "not_patched"


def test_remove_patch_rejects_when_patched_but_no_uninstaller_resolvable(
    tmp_path, monkeypatch,
):
    """Patched (fingerprint present) but neither a per-game uninstaller
    copy nor the shared central script exists — surfaces as not_patched
    rather than a confusing unrelated error."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    monkeypatch.setattr(
        optiscaler_mod, "_UNINSTALLER_SCRIPT", tmp_path / "no-such-uninstaller",
    )
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "OptiScaler.ini").write_text("")
    host = _make_host(str(game_dir))

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.remove_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "not_patched"


def test_remove_patch_rejects_when_install_dir_unresolved():
    host = _make_host("")
    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.remove_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "install_dir_unresolved"


def test_remove_patch_runs_local_uninstaller_script(tmp_path, monkeypatch):
    """Older fgmod builds: per-game uninstaller copy, no args needed."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    uninstaller = game_dir / "fgmod-uninstaller.sh"
    uninstaller.write_text("#!/usr/bin/env bash\n")
    host = _make_host(str(game_dir))

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"Uninstalled\n", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    res = asyncio.run(host.remove_optiscaler_patch("gog", "123"))

    assert res["success"] is True
    assert captured["argv"] == ("bash", str(uninstaller))
    assert captured["cwd"] == str(game_dir)


def test_remove_patch_runs_central_uninstaller_with_resolved_exe(
    tmp_path, monkeypatch,
):
    """Regression: current fgmod builds ship ONE shared uninstaller in
    ~/fgmod/, not a per-game copy. Unlike ``fgmod`` itself, that script has
    no "just pass a directory" mode — it only recognises an argument ending
    in ``.exe`` — so the resolved games.map exe must be passed, and
    STEAM_COMPAT_INSTALL_PATH set as the fallback it itself checks."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    central = tmp_path / "fgmod-uninstaller.sh"
    central.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_UNINSTALLER_SCRIPT", central)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "OptiScaler.ini").write_text("")  # patch fingerprint
    exe = game_dir / "Game.exe"
    exe.write_text("")
    host = _make_host(str(game_dir))
    _with_games_map_exe(host, str(exe))

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"Uninstalled\n", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    res = asyncio.run(host.remove_optiscaler_patch("gog", "123"))

    assert res["success"] is True
    assert captured["argv"] == ("bash", str(central), str(exe))
    assert captured["cwd"] == str(game_dir)
    assert captured["env"]["STEAM_COMPAT_INSTALL_PATH"] == str(game_dir)


def test_remove_patch_runs_central_uninstaller_without_exe_resolved(
    tmp_path, monkeypatch,
):
    """No games.map exe resolvable — still runs the central script (bare,
    relying on its own STEAM_COMPAT_INSTALL_PATH fallback) rather than
    failing outright."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    central = tmp_path / "fgmod-uninstaller.sh"
    central.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_UNINSTALLER_SCRIPT", central)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "OptiScaler.ini").write_text("")
    host = _make_host(str(game_dir))

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    res = asyncio.run(host.remove_optiscaler_patch("gog", "123"))

    assert res["success"] is True
    assert captured["argv"] == ("bash", str(central))
    assert captured["env"]["STEAM_COMPAT_INSTALL_PATH"] == str(game_dir)


def test_remove_patch_prefers_local_over_central_uninstaller(tmp_path, monkeypatch):
    """Both a local per-game copy AND the central script exist — the local
    one wins (matches the vintage of fgmod that patched this specific
    game)."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    central = tmp_path / "central-uninstaller.sh"
    central.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_UNINSTALLER_SCRIPT", central)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    local = game_dir / "fgmod-uninstaller.sh"
    local.write_text("#!/usr/bin/env bash\n")
    host = _make_host(str(game_dir))

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    asyncio.run(host.remove_optiscaler_patch("gog", "123"))

    assert captured["argv"] == ("bash", str(local))


def test_remove_patch_raises_on_nonzero_exit_from_central_script(tmp_path, monkeypatch):
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    central = tmp_path / "fgmod-uninstaller.sh"
    central.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_UNINSTALLER_SCRIPT", central)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "OptiScaler.ini").write_text("")
    host = _make_host(str(game_dir))

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"error\n", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.remove_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "unpatch_failed"


def test_remove_patch_sanitizes_frozen_loader_env(tmp_path, monkeypatch):
    """Same LD_LIBRARY_PATH poisoning fix as apply_optiscaler_patch, but for
    the uninstaller script subprocess (also plain bash)."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "fgmod-uninstaller.sh").write_text("#!/usr/bin/env bash\n")
    host = _make_host(str(game_dir))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")  # noqa: S108
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/pressure-vessel/overrides")

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    asyncio.run(host.remove_optiscaler_patch("gog", "123"))

    assert "LD_LIBRARY_PATH" not in captured["env"]
    assert "LD_LIBRARY_PATH_ORIG" not in captured["env"]


def test_remove_patch_raises_on_nonzero_exit(tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "fgmod-uninstaller.sh").write_text("#!/usr/bin/env bash\n")
    host = _make_host(str(game_dir))

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"error\n", b""

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    with pytest.raises(RpcError) as exc_info:
        asyncio.run(host.remove_optiscaler_patch("gog", "123"))
    assert exc_info.value.code == "unpatch_failed"


def test_remove_patch_rejects_missing_args():
    host = _make_host()
    with pytest.raises(RpcError):
        asyncio.run(host.remove_optiscaler_patch("", "123"))


# ── proxy-DLL auto-configuration (dxgi vs winmm, WINEDLLOVERRIDES) ────────
def _pe_importing(dlls: list[str]) -> bytes:
    """Minimal PE32+ importing the given DLLs (see test_proxy_dll for layout)."""
    import struct

    buf = bytearray(4096)
    buf[0:2] = b"MZ"
    e = 0x80
    struct.pack_into("<I", buf, 0x3C, e)
    buf[e:e + 4] = b"PE\x00\x00"
    coff = e + 4
    struct.pack_into("<H", buf, coff + 2, 1)
    struct.pack_into("<H", buf, coff + 16, 240)
    opt = coff + 20
    struct.pack_into("<H", buf, opt, 0x20B)
    sec = opt + 240
    struct.pack_into("<I", buf, sec + 12, 0)
    struct.pack_into("<I", buf, sec + 16, len(buf))
    struct.pack_into("<I", buf, sec + 20, 0)
    imp = 0x600
    struct.pack_into("<I", buf, opt + 112 + 8, imp)
    cur = imp + (len(dlls) + 1) * 20
    for i, d in enumerate(dlls):
        desc = imp + i * 20
        struct.pack_into("<I", buf, desc + 12, cur)
        struct.pack_into("<I", buf, desc + 16, 0x1000 + i)
        raw = d.encode() + b"\x00"
        buf[cur:cur + len(raw)] = raw
        cur += len(raw)
    return bytes(buf)


def _setup_patch(tmp_path, monkeypatch, exe_name, imported):
    """Common fixture: fgmod installed, a game dir with a dxgi.dll proxy and
    an exe importing ``imported``. Returns (host, game_dir, exe_path)."""
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    fgmod = tmp_path / "fgmod"
    fgmod.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(optiscaler_mod, "_FGMOD_SCRIPT", fgmod)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "dxgi.dll").write_bytes(b"PROXY")  # fgmod's default proxy
    exe = game_dir / exe_name
    exe.write_bytes(_pe_importing(imported))

    host = _make_host(str(game_dir))
    _with_games_map_exe(host, str(exe))

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"Done!\n", b""

    async def _fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return host, game_dir, exe


def test_apply_patch_sets_dxgi_override_for_dx12_game(tmp_path, monkeypatch):
    """A game that statically imports dxgi keeps fgmod's dxgi default and
    gets WINEDLLOVERRIDES=dxgi=n,b written automatically — no rename."""
    host, game_dir, _ = _setup_patch(
        tmp_path, monkeypatch, "game.exe", ["dxgi.dll", "kernel32.dll"],
    )

    res = asyncio.run(host.apply_optiscaler_patch("gog", "123"))

    assert res["proxy_dll"] == "dxgi"
    assert host.config.get("games.gog:123.env_overrides") == {
        "WINEDLLOVERRIDES": "dxgi=n,b",
    }
    # No winmm copy created for a dxgi game.
    assert not (game_dir / "winmm.dll").exists()


def test_apply_patch_renames_proxy_and_sets_winmm_for_nixxes_port(
    tmp_path, monkeypatch,
):
    """Regression this whole feature exists for: a Nixxes-style port imports
    winmm but only loads dxgi dynamically. fgmod's dxgi.dll would never load.
    The patch must copy the proxy to winmm.dll and persist
    WINEDLLOVERRIDES=winmm=n,b so OptiScaler actually loads."""
    host, game_dir, _ = _setup_patch(
        tmp_path, monkeypatch, "Spider-Man.exe", ["winmm.dll", "winhttp.dll"],
    )

    res = asyncio.run(host.apply_optiscaler_patch("gamevault", "22"))

    assert res["proxy_dll"] == "winmm"
    # Proxy copied to the imported name so the loader pulls it in.
    assert (game_dir / "winmm.dll").read_bytes() == b"PROXY"
    assert host.config.get("games.gamevault:22.env_overrides") == {
        "WINEDLLOVERRIDES": "winmm=n,b",
    }


def test_apply_patch_preserves_existing_user_override(tmp_path, monkeypatch):
    """Auto-config must MERGE with a user's existing WINEDLLOVERRIDES, not
    clobber it."""
    host, _, _ = _setup_patch(
        tmp_path, monkeypatch, "Spider-Man.exe", ["winmm.dll"],
    )
    host.config.set(
        "games.gamevault:22.env_overrides",
        {"WINEDLLOVERRIDES": "d3d11=n", "MANGOHUD": "1"},
    )

    asyncio.run(host.apply_optiscaler_patch("gamevault", "22"))

    saved = host.config.get("games.gamevault:22.env_overrides")
    assert saved["MANGOHUD"] == "1"
    assert "winmm=n,b" in saved["WINEDLLOVERRIDES"]
    assert "d3d11=n" in saved["WINEDLLOVERRIDES"]


def test_apply_patch_does_not_overwrite_real_game_dll(tmp_path, monkeypatch):
    """If a real DLL of the chosen proxy name already exists (the game
    shipped it), don't clobber it — the override alone loads OptiScaler's
    copy via native-then-builtin ordering."""
    host, game_dir, _ = _setup_patch(
        tmp_path, monkeypatch, "Spider-Man.exe", ["winmm.dll"],
    )
    (game_dir / "winmm.dll").write_bytes(b"REAL-GAME-WINMM")

    asyncio.run(host.apply_optiscaler_patch("gamevault", "22"))

    # Untouched — we did not overwrite the game's own DLL.
    assert (game_dir / "winmm.dll").read_bytes() == b"REAL-GAME-WINMM"


def test_apply_patch_proxy_autoconfig_failure_does_not_fail_patch(
    tmp_path, monkeypatch,
):
    """A crash inside proxy auto-config must not fail an otherwise-successful
    patch — it degrades to fgmod's dxgi default."""
    host, _, _ = _setup_patch(
        tmp_path, monkeypatch, "game.exe", ["dxgi.dll"],
    )
    import unifideck.rpc.mixins.optiscaler as optiscaler_mod

    def _boom(_exe):
        raise RuntimeError("pe parse exploded")

    monkeypatch.setattr(optiscaler_mod, "pick_proxy_dll", _boom)

    res = asyncio.run(host.apply_optiscaler_patch("gog", "123"))

    assert res["success"] is True
    assert res["proxy_dll"] == "dxgi"
