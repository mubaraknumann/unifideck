"""GOG setup subprocess environment boundaries."""
from __future__ import annotations

from types import SimpleNamespace

from unifideck.launcher.proton.compat.gog_setup import common
from unifideck.launcher.proton.infrastructure import container_escape, setup_run


async def test_run_wine_does_not_start_sidecar_for_setup_helper(monkeypatch):
    """A CheatDeck sidecar belongs to the game, not ``scriptinterpreter``."""
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*cmd, env, stdout, stderr):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["stdout"] = stdout
        captured["stderr"] = stderr

        class Process:
            async def wait(self) -> int:
                return 0

        return Process()

    monkeypatch.setattr(
        setup_run.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(setup_run, "escape_argv", lambda argv, env, cwd: argv)

    plan = SimpleNamespace(
        env={
            "PROTON_REMOTE_DEBUG_CMD": "/home/deck/Games/Trainers/trainer.exe",
            "PRESSURE_VESSEL_FILESYSTEMS_RW": "/home/deck/Games/Trainers",
            "PROTONPATH": "/home/deck/.steam/compatibilitytools.d/GE-Proton11-5",
        },
        python_bin="/usr/bin/python3",
        umu_wrapper="/plugin/bin/umu/umu-run",
    )

    assert await common.run_wine(plan, "scriptinterpreter.exe", ["/VERYSILENT"])

    env = captured["env"]
    assert isinstance(env, dict)
    assert "PROTON_REMOTE_DEBUG_CMD" not in env
    assert env["PRESSURE_VESSEL_FILESYSTEMS_RW"] == "/home/deck/Games/Trainers"
    assert env["PROTONPATH"].endswith("GE-Proton11-5")
    assert plan.env["PROTON_REMOTE_DEBUG_CMD"].endswith("trainer.exe")


async def test_run_wine_does_not_forward_sidecar_through_pressure_vessel(monkeypatch):
    """The escaped command must omit the game sidecar as well as its env."""
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*cmd, env, stdout, stderr):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["stdout"] = stdout
        captured["stderr"] = stderr

        class Process:
            async def wait(self) -> int:
                return 0

        return Process()

    monkeypatch.setattr(setup_run.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(container_escape, "in_pressure_vessel", lambda: True)
    monkeypatch.setattr(
        container_escape.shutil,
        "which",
        lambda name: "/usr/bin/steam-runtime-launch-client",
    )
    monkeypatch.delenv("PROTON_REMOTE_DEBUG_CMD", raising=False)
    monkeypatch.delenv("PRESSURE_VESSEL_FILESYSTEMS_RW", raising=False)

    plan = SimpleNamespace(
        env={
            "PROTON_REMOTE_DEBUG_CMD": "/home/deck/Games/Trainers/trainer.exe",
            "PRESSURE_VESSEL_FILESYSTEMS_RW": "/home/deck/Games/Trainers",
            "PROTONPATH": "/home/deck/.steam/compatibilitytools.d/GE-Proton11-5",
        },
        python_bin="python3",
        umu_wrapper="/plugin/bin/umu/umu-run",
    )

    assert await common.run_wine(plan, "scriptinterpreter.exe", ["/VERYSILENT"])

    cmd = captured["cmd"]
    assert isinstance(cmd, tuple)
    assert not any(arg.startswith("PROTON_REMOTE_DEBUG_CMD=") for arg in cmd)
    assert "PRESSURE_VESSEL_FILESYSTEMS_RW=/home/deck/Games/Trainers" in cmd
