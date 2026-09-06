"""The compat setup steps must run under PROTON_VERB=run, not waitforexitandrun.

Root cause of the install-warmup hang (from Proton's own ``proton`` script,
~L2111): ``waitforexitandrun`` executes ``wineserver -w`` FIRST, which blocks
until any existing wineserver for the prefix exits. Proton's persistent
``steam.exe`` stub keeps that wineserver resident, so the next compat step's
``wineserver -w`` deadlocks (observed: createprefix + regedit each hang 120s,
stacking wineservers on one lock). ``run`` skips ``wineserver -w`` entirely.
These tests pin that the generic compat steps (winetricks, vcruntime regedit)
override the inherited verb to ``run``.

The verb is now applied by ``infrastructure.setup_env.build_setup_env``, which
every setup step shares; ``test_setup_env.py`` pins that shared contract and
the fact that all four sites route through it. These two stay as the
end-to-end check that the override survives each step's own env layering.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unifideck.launcher.proton.compat import vcruntime, winetricks


def _plan(tmp_path):
    """Minimal ProtonLaunchPlan stub carrying the inherited bad verb."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "system.reg").write_text("")  # so compat doesn't early-skip
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "bin").mkdir(parents=True)
    (plugin_dir / "bin" / "vcruntime_fix.reg").write_text("REGEDIT4\n")
    return SimpleNamespace(
        prefix_path=prefix,
        python_bin=Path("/usr/bin/python3"),
        umu_wrapper=Path("/umu/umu-run"),
        # Inherited env deliberately carries the hang-causing verb.
        env={"PROTON_VERB": "waitforexitandrun", "PROTONPATH": "/p"},
        state=SimpleNamespace(proton_tool_id="proton_experimental"),
        context=SimpleNamespace(
            game_id="123", game_key="gog:123", plugin_dir=plugin_dir,
        ),
    )


@pytest.fixture
def captured_env(monkeypatch):
    """Capture the env passed to run_umu_with_retry from each step's module."""
    seen = {}

    async def _fake_winetricks(argv, *, env=None, **kw):
        seen["winetricks"] = dict(env or {})
        return 0

    async def _fake_vcruntime(argv, *, env=None, **kw):
        seen["vcruntime"] = dict(env or {})
        return 0

    monkeypatch.setattr(winetricks, "run_umu_with_retry", _fake_winetricks)
    monkeypatch.setattr(vcruntime, "run_umu_with_retry", _fake_vcruntime)
    return seen


async def test_winetricks_uses_run_verb(tmp_path, captured_env, monkeypatch):
    async def _pkgs(_gid):
        return ["vcrun2022"]

    monkeypatch.setattr(winetricks, "get_required_winetricks", _pkgs)
    monkeypatch.setattr(winetricks, "launcher_toast", lambda *a, **k: None)

    await winetricks.apply_winetricks(_plan(tmp_path))

    assert captured_env["winetricks"]["PROTON_VERB"] == "run"


async def test_vcruntime_uses_run_verb(tmp_path, captured_env):
    await vcruntime.apply_vcruntime_fix(_plan(tmp_path))

    assert captured_env["vcruntime"]["PROTON_VERB"] == "run"
