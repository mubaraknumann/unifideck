"""Redistributables under any Proton, and never under a second one.

umu's winetricks verb execs ``<PROTONPATH>/protonfixes/winetricks``, which
only GE-Proton and UMU-Proton ship. Setup used to borrow GE for that one step
and run the rest under the user's Valve Proton. That left the prefix linked
into two Protons, and the Valve Proton's Wine wrote through the leftover links
into the shared GE install, which the startup audit then re-downloaded on
every start.

Now every step runs under the Proton the game launches on. GE and UMU-Proton
keep umu's verb; a Valve Proton gets the plugin's bundled winetricks run with
that Proton's own Wine (``winetricks_wine``), after its ``winsxs`` links into
the read-only Proton install become real files the VC++ installers can write.
"""
from __future__ import annotations

import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from unifideck.launcher import proton as proton_pkg
from unifideck.launcher.proton import prefix_setup as setup_mod
from unifideck.launcher.proton.compat import prefix_init as prefix_init_mod
from unifideck.launcher.proton.compat import winetricks, winetricks_wine
from unifideck.launcher.proton.compat.winetricks import proton_runs_winetricks


def _proton_dir(root, *, protonfixes: bool):
    """Build a Proton tool dir, optionally with GE's protonfixes payload."""
    root.mkdir(parents=True, exist_ok=True)
    script = root / "proton"
    script.write_text("#!/bin/sh\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    if protonfixes:
        (root / "protonfixes").mkdir()
        (root / "protonfixes" / "winetricks").write_text("#!/bin/sh\n")
    return root


# ── capability check ────────────────────────────────────────────────

def test_ge_proton_with_protonfixes_is_capable(tmp_path):
    assert proton_runs_winetricks(str(_proton_dir(tmp_path / "GE-Proton11-3", protonfixes=True))) is True


def test_official_proton_without_protonfixes_is_not_capable(tmp_path):
    root = _proton_dir(tmp_path / "Proton - Experimental", protonfixes=False)
    assert proton_runs_winetricks(str(root)) is False


def test_anything_it_cannot_judge_fails_open(tmp_path):
    """Only a run certain to fail is skipped; umu reports the rest itself."""
    assert proton_runs_winetricks(None) is True
    assert proton_runs_winetricks("") is True
    assert proton_runs_winetricks(str(tmp_path / "not-there")) is True


def test_a_file_named_protonfixes_does_not_count(tmp_path):
    root = _proton_dir(tmp_path / "Weird-Proton", protonfixes=False)
    (root / "protonfixes").write_text("not a directory")
    assert proton_runs_winetricks(str(root)) is False


# ── the step under a Valve Proton ───────────────────────────────────

def _plan(tmp_path, proton_dir, *, winetricks_bin=True):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    if winetricks_bin:
        (plugin / "bin" / "winetricks").write_text("#!/bin/sh\n")
    _fake_cabextract(plugin / "bin" / "cabextract" / "cabextract")
    return SimpleNamespace(
        prefix_path=prefix,
        python_bin="/usr/bin/python3",
        umu_wrapper="/umu/umu-run",
        env={"PROTONPATH": str(proton_dir)},
        context=SimpleNamespace(game_id="1", game_key="ea:1", plugin_dir=plugin),
    )


def _fake_cabextract(path, *, works=True):
    """A stand-in that answers ``--version`` like the real one, or crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho 'cabextract version 1.11'\n" if works else "#!/bin/sh\nexit 139\n")
    path.chmod(0o755)
    return path


def _valve(tmp_path):
    root = _proton_dir(tmp_path / "Proton - Experimental", protonfixes=False)
    (root / "files" / "bin").mkdir(parents=True)
    for name in ("wine", "wineserver"):
        (root / "files" / "bin" / name).write_text("")
    (root / "files" / "lib" / "wine" / "i386-windows").mkdir(parents=True)
    return root


def _run(monkeypatch, packages=("vcrun2022",)):
    calls = []

    async def _umu(argv, *, env=None, max_attempts=2, **kw):
        calls.append({"argv": argv, "env": env, "attempts": max_attempts})
        return 0

    monkeypatch.setattr(winetricks, "get_required_winetricks", AsyncMock(return_value=list(packages)))
    monkeypatch.setattr(winetricks, "run_umu_with_retry", _umu)
    monkeypatch.setattr(winetricks, "launcher_toast", lambda *a, **k: None)
    monkeypatch.setattr(winetricks, "build_setup_env", lambda plan: {"PATH": "/usr/bin"})
    monkeypatch.setattr(winetricks_wine, "build_setup_env", lambda plan: {"PATH": "/usr/bin"})
    return calls


async def test_a_valve_proton_runs_bundled_winetricks_with_its_own_wine(tmp_path, monkeypatch):
    valve = _valve(tmp_path)
    plan = _plan(tmp_path, valve)
    calls = _run(monkeypatch)

    assert await winetricks.apply_winetricks(plan) is False

    [call] = calls
    assert call["argv"] == ["/bin/sh", str(plan.context.plugin_dir / "bin" / "winetricks"), "-q", "vcrun2022"]
    env = call["env"]
    assert env["WINE"] == env["WINELOADER"] == str(valve / "files" / "bin" / "wine")
    assert env["WINESERVER"] == str(valve / "files" / "bin" / "wineserver")
    assert env["WINEPREFIX"] == str(plan.prefix_path)
    assert env["WINEDLLPATH"] == str(valve / "files" / "lib" / "wine")
    assert env["PATH"].startswith(str(valve / "files" / "bin"))
    assert call["attempts"] == 1, "no umu runtime to retry for"
    assert winetricks.winetricks_pending(plan) is False


async def test_ge_keeps_umus_winetricks_verb(tmp_path, monkeypatch):
    ge = _proton_dir(tmp_path / "GE-Proton11-12", protonfixes=True)
    calls = _run(monkeypatch)

    await winetricks.apply_winetricks(_plan(tmp_path, ge))

    assert calls[0]["argv"][1:4] == ["/umu/umu-run", "winetricks", "-q"]
    assert calls[0]["attempts"] == 2


async def test_missing_bundled_winetricks_fails_the_step_without_running(tmp_path, monkeypatch):
    plan = _plan(tmp_path, _valve(tmp_path), winetricks_bin=False)
    calls = _run(monkeypatch)

    assert await winetricks.apply_winetricks(plan) is False
    assert calls == []
    assert winetricks.winetricks_pending(plan) is False


def test_winsxs_links_into_the_proton_become_writable_copies(tmp_path):
    """The VC++ 2008 installer cannot write over a link into a read-only Proton."""
    valve = _valve(tmp_path)
    dll = valve / "files" / "lib" / "wine" / "i386-windows" / "atl90.dll"
    dll.write_bytes(b"builtin")
    dll.chmod(0o555)
    elsewhere = tmp_path / "other.dll"
    elsewhere.write_bytes(b"x")
    side = tmp_path / "prefix" / "drive_c" / "windows" / "winsxs" / "x86_microsoft.vc90.atl_x"
    side.mkdir(parents=True)
    (side / "atl90.dll").symlink_to(dll)
    (side / "foreign.dll").symlink_to(elsewhere)

    assert winetricks_wine.winsxs_links_to_copies(tmp_path / "prefix", valve) == 1

    copy = side / "atl90.dll"
    assert not copy.is_symlink() and copy.read_bytes() == b"builtin"
    assert copy.stat().st_mode & 0o200
    assert (side / "foreign.dll").is_symlink(), "only links into this Proton"
    assert dll.read_bytes() == b"builtin"


async def test_the_bundled_cabextract_goes_on_winetricks_path(tmp_path, monkeypatch):
    plan = _plan(tmp_path, _valve(tmp_path))
    calls = _run(monkeypatch)
    monkeypatch.setattr(winetricks_wine.shutil, "which", lambda name: None)

    await winetricks.apply_winetricks(plan)

    bundled = plan.context.plugin_dir / "bin" / "cabextract"
    assert calls[0]["env"]["PATH"].split(":")[-1] == str(bundled), "last, so a host tool still wins"


def test_a_broken_bundled_cabextract_falls_back_to_the_hosts(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    _fake_cabextract(plugin / "bin" / "cabextract" / "cabextract", works=False)
    host = _fake_cabextract(tmp_path / "host" / "cabextract")
    monkeypatch.setattr(winetricks_wine.shutil, "which", lambda name: str(host))

    assert winetricks_wine.find_cabextract(plugin) == host


async def test_no_working_cabextract_waits_for_the_next_launch(tmp_path, monkeypatch):
    """No marker and no toast: a broken bundle must not cost the game its libraries."""
    plan = _plan(tmp_path, _valve(tmp_path))
    _fake_cabextract(plan.context.plugin_dir / "bin" / "cabextract" / "cabextract", works=False)
    monkeypatch.setattr(winetricks_wine.shutil, "which", lambda name: None)
    calls = _run(monkeypatch)
    toasts = []
    monkeypatch.setattr(winetricks, "launcher_toast", lambda *a, **k: toasts.append(a))

    assert await winetricks.apply_winetricks(plan) is False
    assert calls == [] and toasts == []
    assert winetricks.winetricks_pending(plan) is True


# ── setup never borrows a second Proton ─────────────────────────────

async def test_setup_runs_every_step_under_the_chosen_proton(tmp_path, monkeypatch):
    """The user's Valve Proton choice: one Proton for createprefix, compat and launch."""
    valve = _proton_dir(tmp_path / "Proton - Experimental", protonfixes=False)
    managed_ge = MagicMock(return_value=(str(tmp_path / "GE"), "GE-Proton11-3"))

    monkeypatch.setattr(proton_pkg, "find_python_3_10_plus", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        proton_pkg, "proton_prepare",
        lambda ctx, state, **kw: SimpleNamespace(
            tool_id=kw["proton_tool_id"], env={}, context=ctx,
            prefix_path=tmp_path / "prefix",
        ),
    )
    monkeypatch.setattr(
        proton_pkg, "select_proton_with_source",
        lambda *a, **k: (valve / "proton", "proton_experimental", "steam"),
    )
    monkeypatch.setattr(proton_pkg, "select_managed_ge_proton", managed_ge)
    monkeypatch.setattr(prefix_init_mod, "ensure_prefix_initialized", AsyncMock())
    monkeypatch.setattr(setup_mod, "_compat_pending", lambda *a, **k: True)

    ran_under = []

    async def _compat(plan):
        ran_under.append(plan.tool_id)
        return False

    from unifideck.launcher.proton import compat as compat_pkg
    monkeypatch.setattr(compat_pkg, "apply_prefix_compat", _compat)

    ctx = SimpleNamespace(
        store="gog", game_id="123", game_key="gog:123", steam_app_id=None, proton_tool=None,
    )
    result = await setup_mod.setup_prefix(ctx, SimpleNamespace())

    assert ran_under == ["proton_experimental"]
    assert result == ("proton_experimental", False)
    managed_ge.assert_not_called()
