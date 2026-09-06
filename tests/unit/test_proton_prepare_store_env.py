"""Regression: Epic launches must never expose STORE=egs to ProtonFixes.

ProtonFixes' EGS-store defaults are actively harmful, not just redundant:
they re-run vcrun2022 (core-dumps inside pressure-vessel) and — the one
that actually breaks launches — add a HKCR\\com.epicgames.launcher registry
key that makes the EOS SDK switch to launcher-IPC auth mode, causing an
instant exit/hang for non-Ubisoft Epic titles that use EOS (the retired
bash launcher forced STORE=none for exactly this reason).

Field case: Kingdom Hearts Re:Chain of Memories never launched, on any
Proton version, even after deleting and recreating the prefix — because
compat/vcruntime.py's regedit step (and prefix_init.py's createprefix, and
winetricks.py) all build their env as ``dict(plan.env)``, which carried
STORE=egs generically. That poisoned the prefix's registry on the very
first setup step, before the actual game ever ran — so a fresh prefix hit
the exact same corruption immediately. build_legendary_env() already forced
STORE=none for the final legendary invocation, but that was too late: the
damage was already done by the earlier setup steps sharing the same env.
"""
from __future__ import annotations

from pathlib import Path

from unifideck.launcher.proton.infrastructure import core
from unifideck.launcher.types.context import LaunchContext, RuntimeState


def _prepare(
    tmp_path, monkeypatch, store, umu_id=None, game_id="game1", exe_name="null",
):
    ctx = LaunchContext(
        store=store,
        game_id=game_id,
        exe_path=Path("/dev") / exe_name,
        work_dir=tmp_path,
        plugin_dir=tmp_path,
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setattr(core, "_resolve_prefix", lambda c: prefix)
    monkeypatch.setattr(core, "_lookup_umu_id", lambda c, s, p: umu_id)
    monkeypatch.setattr(
        core, "_locate_umu_wrapper", lambda p, d: tmp_path / "umu-run",
    )
    return core.proton_prepare(
        ctx, RuntimeState(),
        python_bin=Path("/usr/bin/python3"),
        proton_path=tmp_path / "proton",
        proton_tool_id="GE-Proton10-34",
    )


def test_epic_launch_forces_store_none(tmp_path, monkeypatch):
    plan = _prepare(tmp_path, monkeypatch, "epic")
    assert plan.env["STORE"] == "none"
    # Diagnostics still record the real store code.
    assert plan.state.umu_store_code == "egs"


def test_gog_launch_keeps_real_store(tmp_path, monkeypatch):
    plan = _prepare(tmp_path, monkeypatch, "gog")
    assert plan.env["STORE"] == "gog"


def test_ubisoft_launch_keeps_real_store(tmp_path, monkeypatch):
    plan = _prepare(tmp_path, monkeypatch, "ubisoft")
    assert plan.env["STORE"] == "ubisoft"


# ── Rockstar-on-Epic (RDR2/GTA5) — the one Epic case wanting STORE=egs ──

def test_ordinary_epic_game_unchanged(tmp_path, monkeypatch):
    """A non-Rockstar Epic game must NOT trigger any Rockstar handling.

    Regression guard: STORE stays "none" and no WINEDLLOVERRIDES is added.
    """
    plan = _prepare(tmp_path, monkeypatch, "epic", game_id="Fortnite")
    assert plan.env["STORE"] == "none"
    assert "WINEDLLOVERRIDES" not in plan.env


def test_rockstar_rdr2_gets_egs_with_umu_id_none(tmp_path, monkeypatch):
    """The REAL Decky-build case: game_id='Heather', umu_id=None (no
    umu_lookup.py). This is the exact runtime the tester hit — the gate
    MUST fire on the Epic app name, not the (always-None) umu id.
    """
    plan = _prepare(tmp_path, monkeypatch, "epic", umu_id=None, game_id="Heather")
    assert plan.env["STORE"] == "egs"
    assert plan.env["WINEDLLOVERRIDES"] == "vulkan-1=n,b"


def test_rockstar_gta5_gets_egs_by_app_name(tmp_path, monkeypatch):
    plan = _prepare(
        tmp_path, monkeypatch, "epic", umu_id=None,
        game_id="9d2d0eb64d5c44529cece33fe2a46482",
    )
    assert plan.env["STORE"] == "egs"
    assert plan.env["WINEDLLOVERRIDES"] == "vulkan-1=n,b"


def test_rockstar_gta5_enhanced_edition_gets_egs_by_app_name(tmp_path, monkeypatch):
    """UD report: GTA V's "Enhanced Edition" is a separate Epic catalog id
    from the legacy "Grand Theft Auto V" tested above — it was missing from
    the allowlist entirely, so this title got none of STORE=egs,
    WINEDLLOVERRIDES, the fake launcher, or the epic_cleanup skip, and the
    Rockstar Games Launcher couldn't re-verify the install after first boot.
    """
    plan = _prepare(
        tmp_path, monkeypatch, "epic", umu_id=None,
        game_id="8769e24080ea413b8ebca3f1b8c50951",
    )
    assert plan.env["STORE"] == "egs"
    assert plan.env["WINEDLLOVERRIDES"] == "vulkan-1=n,b"


def test_rockstar_matches_by_exe_name_with_unknown_app_id(tmp_path, monkeypatch):
    """The durable fallback: even an Epic app id the allowlist has never
    seen still gets the Rockstar flow, purely from the Play-launcher exe
    name — this is what makes the fix resilient to the NEXT Rockstar/Epic
    catalog reshuffle instead of needing another hardcoded id.
    """
    plan = _prepare(
        tmp_path, monkeypatch, "epic", umu_id=None,
        game_id="some-future-epic-catalog-id", exe_name="PlayGTAV.exe",
    )
    assert plan.env["STORE"] == "egs"
    assert plan.env["WINEDLLOVERRIDES"] == "vulkan-1=n,b"


def test_rockstar_umu_id_secondary_still_matches(tmp_path, monkeypatch):
    """If umu_lookup.py IS present and returns a Rockstar umu id, the
    secondary match still triggers the flow (belt-and-suspenders).
    """
    plan = _prepare(
        tmp_path, monkeypatch, "epic", umu_id="umu-1174180", game_id="game1",
    )
    assert plan.env["STORE"] == "egs"
    assert plan.env["WINEDLLOVERRIDES"] == "vulkan-1=n,b"


def test_rockstar_app_name_on_non_epic_store_is_not_special(tmp_path, monkeypatch):
    """The gate is store==epic AND rockstar identity — a GOG game named
    'Heather' (hypothetically) must keep its own store profile.
    """
    plan = _prepare(tmp_path, monkeypatch, "gog", game_id="Heather")
    assert plan.env["STORE"] == "gog"
    assert "WINEDLLOVERRIDES" not in plan.env


# ── Titles that bundle their own ICU (Cyberpunk 2077) ──────────────────
#
# Field case (2026-08-12 bundle): every Cyberpunk launch that reached the
# game aborted on ``icuuc.dll.u_setMemoryFunctions_65`` — Wine's builtin
# icuuc.dll stub, new in GE-Proton11-3, shadowing the ICU 65 the game ships.
# The window opens and stays blank. Proton's own bundled ICU is 68, so only
# loading the game's own copy fixes it.
#
# GE-Proton11-3 shipped builtin stubs for ``icuuc.dll`` AND ``icuin.dll`` in
# the same build, and Cyberpunk bundles ICU 65 as both. Overriding icuuc
# alone only moved the abort one library along — a later bundle (2026-08-23)
# shows the game dying on ``icuin.dll.?compile@RegexPattern@icu_65@@...``
# instead. Both must be overridden together.

def test_cyberpunk_gog_gets_native_icu_override(tmp_path, monkeypatch):
    plan = _prepare(
        tmp_path, monkeypatch, "gog",
        game_id="1423049311", exe_name="REDprelauncher.exe",
    )
    assert plan.env["WINEDLLOVERRIDES"] == "icuuc=n,b;icuin=n,b"
    assert plan.env["STORE"] == "gog"


def test_cyberpunk_matches_on_exe_name_alone(tmp_path, monkeypatch):
    """Primary tier: the exe name, for the direct-exe retry path and for
    any store/edition whose id we do not hold."""
    plan = _prepare(
        tmp_path, monkeypatch, "gog",
        game_id="some-unknown-id", exe_name="Cyberpunk2077.exe",
    )
    assert plan.env["WINEDLLOVERRIDES"] == "icuuc=n,b;icuin=n,b"


def test_cyberpunk_epic_codename_matches(tmp_path, monkeypatch):
    """Same title on Epic ships the same bundled ICU, so it needs the same
    override — the gate is deliberately store-independent."""
    plan = _prepare(
        tmp_path, monkeypatch, "epic", game_id="Ginger", exe_name="null",
    )
    assert plan.env["WINEDLLOVERRIDES"] == "icuuc=n,b;icuin=n,b"


def test_ordinary_gog_game_gets_no_icu_override(tmp_path, monkeypatch):
    """Regression guard: a non-matching launch is left completely alone."""
    plan = _prepare(
        tmp_path, monkeypatch, "gog",
        game_id="1207658883", exe_name="AoW.exe",
    )
    assert "WINEDLLOVERRIDES" not in plan.env


def test_icu_override_merges_with_existing(tmp_path, monkeypatch):
    """Must not clobber overrides another step already set (Proton appends
    its own long default list, and battlenet sets locationapi=d)."""
    monkeypatch.setenv("WINEDLLOVERRIDES", "locationapi=d")
    plan = _prepare(
        tmp_path, monkeypatch, "gog",
        game_id="1423049311", exe_name="REDprelauncher.exe",
    )
    assert plan.env["WINEDLLOVERRIDES"] == "locationapi=d;icuuc=n,b;icuin=n,b"


def test_icu_override_fills_in_only_the_missing_dll(tmp_path, monkeypatch):
    """The skip guard is per DLL, not "did we already mention any ICU".

    A single ``"icuuc" in existing`` test used to short-circuit the whole
    block, so an environment that already named icuuc got NO icuin — the
    half-applied state Cyberpunk aborts in.
    """
    monkeypatch.setenv("WINEDLLOVERRIDES", "icuuc=n,b")
    plan = _prepare(
        tmp_path, monkeypatch, "gog",
        game_id="1423049311", exe_name="REDprelauncher.exe",
    )
    assert plan.env["WINEDLLOVERRIDES"] == "icuuc=n,b;icuin=n,b"


def test_icu_override_is_a_noop_when_both_already_present(tmp_path, monkeypatch):
    monkeypatch.setenv("WINEDLLOVERRIDES", "icuin=b;icuuc=b")
    plan = _prepare(
        tmp_path, monkeypatch, "gog",
        game_id="1423049311", exe_name="REDprelauncher.exe",
    )
    assert plan.env["WINEDLLOVERRIDES"] == "icuin=b;icuuc=b"


def test_user_env_override_still_wins(tmp_path, monkeypatch):
    """ctx.env_overrides is applied last, so an explicit user value wins."""
    ctx = LaunchContext(
        store="gog",
        game_id="1423049311",
        exe_path=Path("/dev/REDprelauncher.exe"),
        work_dir=tmp_path,
        plugin_dir=tmp_path,
        env_overrides={"WINEDLLOVERRIDES": "icuuc=b"},
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setattr(core, "_resolve_prefix", lambda c: prefix)
    monkeypatch.setattr(core, "_lookup_umu_id", lambda c, s, p: None)
    monkeypatch.setattr(
        core, "_locate_umu_wrapper", lambda p, d: tmp_path / "umu-run",
    )
    plan = core.proton_prepare(
        ctx, RuntimeState(),
        python_bin=Path("/usr/bin/python3"),
        proton_path=tmp_path / "proton",
        proton_tool_id="GE-Proton11-3",
    )
    assert plan.env["WINEDLLOVERRIDES"] == "icuuc=b"
