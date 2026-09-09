"""Every prefix-setup helper must diverge from the game env the same way.

PR #449 stripped the CheatDeck sidecar (``PROTON_REMOTE_DEBUG_CMD``) from the
GOG setup helper only, because that is the one site with an unbounded
``proc.wait()``. The other three — createprefix, winetricks, the vcruntime
regedit — still inherited it and would each spawn-and-leak a trainer into the
prefix, burning their whole timeout budget on a CheatDeck title.

These tests pin the shared ``build_setup_env`` contract and, more importantly,
pin that all four call sites actually go through it, so a fifth divergence
cannot be added to one site and silently missed by the rest.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unifideck.launcher.proton.compat import prefix_init, vcruntime, winetricks
from unifideck.launcher.proton.compat.gog_setup import common
from unifideck.launcher.proton.infrastructure import setup_run
from unifideck.launcher.proton.infrastructure.setup_env import build_setup_env

_SIDECAR = "/home/deck/Games/Trainers/trainer.exe"


def _game_env() -> dict[str, str]:
    """The env a CheatDeck-configured game launch carries."""
    return {
        "PROTON_REMOTE_DEBUG_CMD": _SIDECAR,
        "PRESSURE_VESSEL_FILESYSTEMS_RW": "/home/deck/Games/Trainers",
        "PROTONPATH": "/home/deck/.steam/compatibilitytools.d/GE-Proton11-5",
        "STEAM_COMPAT_DATA_PATH": "/home/deck/prefixes/gog-123",
        # The verb a setup step must never inherit.
        "PROTON_VERB": "waitforexitandrun",
        "GAMEID": "umu-1234567",
    }


# ── build_setup_env (pure) ─────────────────────────────────────────────


def test_build_setup_env_drops_the_game_only_sidecar() -> None:
    env = build_setup_env(SimpleNamespace(env=_game_env()))
    assert "PROTON_REMOTE_DEBUG_CMD" not in env


def test_build_setup_env_applies_the_generic_setup_identity() -> None:
    env = build_setup_env(SimpleNamespace(env=_game_env()))
    assert env["GAMEID"] == "umu-0"
    assert env["PROTON_VERB"] == "run"


def test_build_setup_env_preserves_prefix_and_container_access() -> None:
    """Dropping these would cost the helper its prefix or its filesystem."""
    env = build_setup_env(SimpleNamespace(env=_game_env()))
    assert env["PRESSURE_VESSEL_FILESYSTEMS_RW"] == "/home/deck/Games/Trainers"
    assert env["PROTONPATH"].endswith("GE-Proton11-5")
    assert env["STEAM_COMPAT_DATA_PATH"] == "/home/deck/prefixes/gog-123"


def test_build_setup_env_does_not_mutate_the_game_env() -> None:
    """The real launch still gets its sidecar and its inherited verb."""
    plan = SimpleNamespace(env=_game_env())
    build_setup_env(plan)
    assert plan.env["PROTON_REMOTE_DEBUG_CMD"] == _SIDECAR
    assert plan.env["PROTON_VERB"] == "waitforexitandrun"


# ── every call site routes through it ──────────────────────────────────


def _plan(tmp_path):
    """A plan stub accepted by all four setup sites."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "system.reg").write_text("")
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "bin").mkdir(parents=True)
    (plugin_dir / "bin" / "vcruntime_fix.reg").write_text("REGEDIT4\n")
    return SimpleNamespace(
        prefix_path=prefix,
        python_bin=Path("/usr/bin/python3"),
        umu_wrapper=Path("/umu/umu-run"),
        env=_game_env(),
        state=SimpleNamespace(proton_tool_id="proton_experimental"),
        context=SimpleNamespace(
            game_id="123", game_key="gog:123", plugin_dir=plugin_dir,
        ),
    )


@pytest.fixture
def captured_env(monkeypatch):
    """Capture the env each setup step hands to its umu runner."""
    seen: dict[str, dict[str, str]] = {}

    async def _fake_winetricks(argv, *, env=None, **kw):
        seen["winetricks"] = dict(env or {})
        return 0

    async def _fake_vcruntime(argv, *, env=None, **kw):
        seen["vcruntime"] = dict(env or {})
        return 0

    monkeypatch.setattr(winetricks, "run_umu_with_retry", _fake_winetricks)
    monkeypatch.setattr(vcruntime, "run_umu_with_retry", _fake_vcruntime)
    return seen


async def test_winetricks_drops_the_sidecar(tmp_path, captured_env, monkeypatch):
    async def _pkgs(_gid):
        return ["vcrun2022"]

    monkeypatch.setattr(winetricks, "get_required_winetricks", _pkgs)
    monkeypatch.setattr(winetricks, "launcher_toast", lambda *a, **k: None)

    await winetricks.apply_winetricks(_plan(tmp_path))

    env = captured_env["winetricks"]
    assert "PROTON_REMOTE_DEBUG_CMD" not in env
    assert env["PROTON_VERB"] == "run"
    # winetricks' own extras must survive the shared base.
    assert env["UMU_RUNTIME_UPDATE"] == "0"
    assert env["WINEPREFIX"]


async def test_vcruntime_drops_the_sidecar(tmp_path, captured_env):
    await vcruntime.apply_vcruntime_fix(_plan(tmp_path))

    env = captured_env["vcruntime"]
    assert "PROTON_REMOTE_DEBUG_CMD" not in env
    assert env["PROTON_VERB"] == "run"


async def test_gog_setup_run_wine_drops_the_sidecar(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_exec(*cmd, env, stdout, stderr):
        captured["env"] = env

        class Process:
            async def wait(self) -> int:
                return 0

        return Process()

    monkeypatch.setattr(setup_run.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(setup_run, "escape_argv", lambda argv, env, cwd: argv)

    assert await common.run_wine(_plan(tmp_path), "scriptinterpreter.exe", [])

    env = captured["env"]
    assert isinstance(env, dict)
    assert "PROTON_REMOTE_DEBUG_CMD" not in env
    # The GOG helper's own extra must survive the shared base.
    assert env["STORE"] == "gog"


async def test_prefix_init_createprefix_drops_the_sidecar(tmp_path, monkeypatch):
    """createprefix is the FIRST setup step, so it leaks the trainer earliest."""
    captured: dict[str, object] = {}

    async def fake_createprefix(_plan_arg, env, _prefix_root):
        captured["env"] = dict(env)
        return True

    monkeypatch.setattr(
        prefix_init, "_run_createprefix_with_retry", fake_createprefix,
    )
    monkeypatch.setattr(prefix_init, "ensure_umu_runtime_ready", lambda: None)
    monkeypatch.setattr(prefix_init, "launcher_toast", lambda *a, **k: None)

    async def _noop_saves(*a, **k):
        return None

    monkeypatch.setattr(prefix_init, "restore_or_migrate_saves", _noop_saves)

    # An uninitialised prefix root — with a system.reg, _ensure_created
    # early-returns and never builds an env at all.
    fresh = tmp_path / "fresh-prefix"
    fresh.mkdir()

    await prefix_init._ensure_created(_plan(tmp_path), fresh)

    env = captured["env"]
    assert isinstance(env, dict)
    assert "PROTON_REMOTE_DEBUG_CMD" not in env
    assert env["PROTON_VERB"] == "run"
    assert env["GAMEID"] == "umu-0"
