"""Official (Valve) Proton resolution from Steam's internal tool ids.

Field bug: a user selecting an official Proton in Steam's own
Properties > Compatibility dialog had their choice silently ignored — every
launch logged ``selected via saved tool: GE-Proton11-3`` while ``config.vdf``
recorded ``proton_11``.

Two causes, both fixed here:

1. Official Protons ship no ``compatibilitytool.vdf``, so nothing declares
   their internal id and the ``compatibilitytools.d`` roots can never match
   them (they live in ``steamapps/common`` under a *display-name* dir).
2. ``selector`` compensated with a hardcoded id->dirname table that stopped
   at ``proton_10``. Once Proton 11 shipped, ``proton_11`` matched nothing,
   fell through every tier, and launched under GE-Proton instead.

Both now derive the id from the installed directory name, so any past or
future release resolves with no table to maintain.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from unifideck.launcher.proton.infrastructure import selector as S
from unifideck.utils import vdf_compat as vc

# (directory name as Steam installs it, internal CompatToolMapping id)
_OFFICIAL = [
    ("Proton - Experimental", "proton_experimental"),
    ("Proton Hotfix", "proton_hotfix"),
    ("Proton 11.0", "proton_11"),
    ("Proton 10.0", "proton_10"),
    ("Proton 9.0 (Beta)", "proton_9"),
    ("Proton 8.0", "proton_8"),
    ("Proton 7.0", "proton_7"),
]


@pytest.mark.parametrize(("dir_name", "expected"), _OFFICIAL)
def test_alias_derived_from_dir_name(dir_name: str, expected: str):
    assert vc.official_proton_alias(dir_name) == expected


@pytest.mark.parametrize(
    "dir_name",
    ["GE-Proton11-3", "GE-Proton9-13", "UMU-Proton-9.0-4e", "Proton", "sniper"],
)
def test_no_alias_for_third_party_or_bare(dir_name: str):
    """Third-party builds declare their own id via manifest; never alias them."""
    assert vc.official_proton_alias(dir_name) is None


def _library(tmp_path: Path, *dir_names: str) -> Path:
    common = tmp_path / "steamapps" / "common"
    for name in dir_names:
        d = common / name
        d.mkdir(parents=True)
        (d / "proton").write_text("#!/bin/sh\n")
    return common


@pytest.mark.parametrize(("dir_name", "tool_id"), _OFFICIAL)
def test_resolve_proton_path_finds_official_by_internal_id(
    tmp_path, monkeypatch, dir_name: str, tool_id: str,
):
    common = _library(tmp_path, dir_name)
    monkeypatch.setattr(S, "STEAM_LIBRARY_ROOTS", [str(common)])
    monkeypatch.setattr(S, "_compat_tool_roots", lambda: [])
    monkeypatch.setattr(S, "_discovered_library_commons", lambda: [])
    assert S.resolve_proton_path(tool_id) == common / dir_name / "proton"


def test_proton_11_regression(tmp_path, monkeypatch):
    """The exact field regression: proton_11 must resolve, not fall through.

    Pinned separately from the parametrised sweep because this is the case
    the retired hardcoded table missed.
    """
    common = _library(
        tmp_path, "Proton 11.0", "Proton 10.0", "Proton - Experimental",
    )
    monkeypatch.setattr(S, "STEAM_LIBRARY_ROOTS", [str(common)])
    monkeypatch.setattr(S, "_compat_tool_roots", lambda: [])
    monkeypatch.setattr(S, "_discovered_library_commons", lambda: [])
    assert S.resolve_proton_path("proton_11") == common / "Proton 11.0" / "proton"


def test_future_proton_version_resolves_without_code_change(
    tmp_path, monkeypatch,
):
    """A Proton newer than anything this code knows about still resolves."""
    common = _library(tmp_path, "Proton 14.0")
    monkeypatch.setattr(S, "STEAM_LIBRARY_ROOTS", [str(common)])
    monkeypatch.setattr(S, "_compat_tool_roots", lambda: [])
    monkeypatch.setattr(S, "_discovered_library_commons", lambda: [])
    assert S.resolve_proton_path("proton_14") == common / "Proton 14.0" / "proton"


def test_verbatim_dir_name_still_resolves(tmp_path, monkeypatch):
    """A tool whose dir name IS the id (GE dropped into common/) still works."""
    common = _library(tmp_path, "GE-Proton11-3")
    monkeypatch.setattr(S, "STEAM_LIBRARY_ROOTS", [str(common)])
    monkeypatch.setattr(S, "_compat_tool_roots", lambda: [])
    monkeypatch.setattr(S, "_discovered_library_commons", lambda: [])
    assert (
        S.resolve_proton_path("GE-Proton11-3")
        == common / "GE-Proton11-3" / "proton"
    )


def test_uninstalled_official_proton_returns_none(tmp_path, monkeypatch):
    """Not installed → None, so the caller falls through to the next tier."""
    common = _library(tmp_path, "Proton 10.0")
    monkeypatch.setattr(S, "STEAM_LIBRARY_ROOTS", [str(common)])
    monkeypatch.setattr(S, "_compat_tool_roots", lambda: [])
    monkeypatch.setattr(S, "_discovered_library_commons", lambda: [])
    assert S.resolve_proton_path("proton_11") is None


def test_iter_compat_tools_keys_official_alias(tmp_path):
    """``iter_compat_tools`` also exposes the alias for bare Proton dirs."""
    common = _library(tmp_path, "Proton 11.0")
    tools = vc.iter_compat_tools([common])
    assert "proton_11" in tools
    assert "Proton 11.0" in tools


def _stub_tiers(
    monkeypatch, tmp_path, *, live: str | None, shadow: str | None,
    installed: tuple[str, ...] = (),
):
    """Stub the two top tiers; only ``installed`` tools resolve."""
    common = _library(tmp_path, *installed) if installed else tmp_path / "empty"
    monkeypatch.setattr(S, "get_steam_compat_tool_override", lambda _a: live)
    monkeypatch.setattr(S, "get_saved_proton_tool", lambda _g: shadow)
    monkeypatch.setattr(S, "get_unifideck_proton_tool", lambda: None)
    monkeypatch.setattr(S, "get_global_default_tool", lambda: None)
    monkeypatch.setattr(S, "STEAM_LIBRARY_ROOTS", [str(common)])
    monkeypatch.setattr(S, "_compat_tool_roots", lambda: [])
    monkeypatch.setattr(S, "_discovered_library_commons", lambda: [])
    monkeypatch.setattr(
        S.ge_installer, "proton_install_problem", lambda _p: None,
    )


def test_live_steam_pick_beats_stale_saved_shadow(tmp_path, monkeypatch):
    """The exact Limbo field bug: a stale shadow must not override the
    Proton the user just selected in Steam's own dialog."""
    _stub_tiers(
        monkeypatch, tmp_path, live="GE-Proton9-26", shadow="GE-Proton11-3",
        installed=("GE-Proton9-26", "GE-Proton11-3"),
    )
    _, tool = S.select_proton_version(
        steam_app_id="2675664683", store_game_id="epic:Hazelnut",
    )
    assert tool == "GE-Proton9-26"


def test_saved_shadow_used_when_steam_side_cleared(tmp_path, monkeypatch):
    """With no live entry (the state the frontend leaves behind after it
    clears Steam's side), the shadow still carries the user's choice."""
    _stub_tiers(
        monkeypatch, tmp_path, live=None, shadow="GE-Proton11-3",
        installed=("GE-Proton11-3",),
    )
    _, tool = S.select_proton_version(
        steam_app_id="2675664683", store_game_id="epic:Hazelnut",
    )
    assert tool == "GE-Proton11-3"


def test_unresolvable_live_pick_falls_through_to_shadow(tmp_path, monkeypatch):
    """A live entry naming an uninstalled tool must not dead-end the launch."""
    _stub_tiers(
        monkeypatch, tmp_path, live="GE-Proton-Deleted", shadow="GE-Proton11-3",
        installed=("GE-Proton11-3",),
    )
    _, tool = S.select_proton_version(
        steam_app_id="2675664683", store_game_id="epic:Hazelnut",
    )
    assert tool == "GE-Proton11-3"


def test_proton_found_in_secondary_library(tmp_path, monkeypatch):
    """Proton installed on an SD card / second drive must still resolve.

    Steam puts Proton in whichever library it chose; only searching the main
    install silently loses a perfectly valid user selection.
    """
    main = tmp_path / "main"
    sd = tmp_path / "sdcard"
    (main / "steamapps").mkdir(parents=True)
    common = sd / "steamapps" / "common" / "Proton 11.0"
    common.mkdir(parents=True)
    (common / "proton").write_text("#!/bin/sh\n")
    (main / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % (main, sd),
    )
    # Discovery is deliberately NOT stubbed here — it is what's under test.
    # Pointing find_steam_root at tmp_path isolates it from the host.
    monkeypatch.setattr(vc, "find_steam_root", lambda: main)
    monkeypatch.setattr(S, "STEAM_LIBRARY_ROOTS", [])
    monkeypatch.setattr(S, "_compat_tool_roots", lambda: [])

    assert sd in vc.steam_library_dirs()
    assert S.resolve_proton_path("proton_11") == common / "proton"


def test_library_dirs_degrade_without_libraryfolders(tmp_path, monkeypatch):
    """A missing/unreadable libraryfolders.vdf still yields the main root."""
    main = tmp_path / "main"
    main.mkdir()
    monkeypatch.setattr(vc, "find_steam_root", lambda: main)
    assert vc.steam_library_dirs() == [main]

    monkeypatch.setattr(vc, "find_steam_root", lambda: None)
    assert vc.steam_library_dirs() == []


def test_third_party_proton_whose_dir_name_differs_from_steam_name(tmp_path):
    """Custom Protons where the id Steam stores != the directory name.

    Straight from a field bundle (Flatpak Steam desktop): ``CompatToolMapping``
    held ``proton-cachyos-slr`` and ``cachyos_11.0_20260702-LinUwUx-proton``
    while the dirs were ``proton-cachyos-11.0-20260703-slr-x86_64_v3`` and
    ``cachyos_11.0_20260702-LinUwUx``. Resolution must go through the
    ``compatibilitytool.vdf`` manifest, not the folder name — the "any Proton,
    including third-party" requirement. A half-finished download dir alongside
    them must not break the scan either.
    """
    root = tmp_path / "compatibilitytools.d"
    root.mkdir()
    cases = [
        ("proton-cachyos-11.0-20260703-slr-x86_64_v3", "proton-cachyos-slr"),
        ("cachyos_11.0_20260702-LinUwUx",
         "cachyos_11.0_20260702-LinUwUx-proton"),
        ("GE-Proton11-1-LinUwUx", "GE-Proton11-1-LinUwUx"),
    ]
    for dir_name, internal in cases:
        d = root / dir_name
        d.mkdir()
        (d / "proton").write_text("#!/bin/sh\n")
        (d / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n\t"compat_tools"\n\t{\n'
            f'\t\t"{internal}"\n\t\t{{\n'
            '\t\t\t"install_path"\t"."\n'
            '\t\t}\n\t}\n}\n',
        )
    (root / ".GE-Proton11-3.dl-p_36va1k").mkdir()  # partial download

    for dir_name, internal in cases:
        resolved = vc.resolve_compat_tool(internal, [root])
        assert resolved == root / dir_name / "proton", internal


def test_manifest_declared_id_wins_over_derived_alias(tmp_path):
    """A dir with a real manifest keeps its declared id (setdefault order)."""
    d = tmp_path / "Proton-CachyOS"
    d.mkdir()
    (d / "proton").write_text("#!/bin/sh\n")
    (d / "compatibilitytool.vdf").write_text(
        '"compatibilitytools"\n{\n\t"compat_tools"\n\t{\n'
        '\t\t"proton-cachyos"\n\t\t{\n'
        '\t\t\t"install_path"\t"."\n'
        '\t\t}\n\t}\n}\n',
    )
    tools = vc.iter_compat_tools([tmp_path])
    assert tools["proton-cachyos"] == d / "proton"


# ── the user's Force-Compat pick must actually REACH the selector ──────

def test_signed_appid_normalises_to_unsigned_lookup(tmp_path, monkeypatch):
    """``games.map``/``shortcuts.vdf`` store the SIGNED appid, but
    ``CompatToolMapping`` is keyed by the UNSIGNED one. Passing the signed
    form straight through matched nothing and silently dropped the pick."""
    cfg = tmp_path / "config.vdf"
    cfg.write_text(
        '"InstallConfigStore"\n{\n"Software" { "Valve" { "Steam"\n{\n'
        '  "CompatToolMapping"\n  {\n'
        '    "0" { "name" "proton_experimental" }\n'
        '    "2284541373" { "name" "GE-Proton9-13" }\n'
        '  }\n} } }\n}\n',
    )
    monkeypatch.setattr(S.vdf_compat, "find_steam_config_vdf", lambda: cfg)
    signed = 2284541373 - 2**32
    assert S.get_steam_compat_tool_override(str(signed)) == "GE-Proton9-13"
    assert S.get_steam_compat_tool_override("2284541373") == "GE-Proton9-13"


async def test_launch_context_carries_steam_app_id():
    """``ctx.steam_app_id`` was never populated, so ``select_proton_version``'s
    ``if steam_app_id`` guard skipped the user's Force-Compat tier on EVERY
    launch — their Properties > Compatibility choice was never read at all."""
    from unifideck.launcher import dispatcher as D

    class _Entry:
        exe = "/games/g/g.exe"
        work_dir = "/games/g"
        app_id = 2284541373 - 2**32

    class _Svc:
        async def get_entry_for_game_key(self, _s, _g):
            return _Entry()

    exe, work_dir, has_entry, app_id = await D._resolve_game_exe(
        _Svc(), "epic", "gid", "epic:gid",
    )
    assert has_entry and app_id == 2284541373 - 2**32
    ctx = D._game_context("epic", "gid", exe, work_dir, "", app_id)
    assert ctx.steam_app_id == str(2284541373 - 2**32)


async def test_launch_context_steam_app_id_none_without_games_map_row():
    """No row → no appid → the tier is correctly skipped rather than
    looking up appid 0."""
    from unifideck.launcher import dispatcher as D

    class _Svc:
        async def get_entry_for_game_key(self, _s, _g):
            return None

    monkey_exe, work_dir, has_entry, app_id = await D._resolve_game_exe(
        _Svc(), "microsoft", "gid", "microsoft:gid",
    )
    assert not has_entry and app_id == 0
    ctx = D._game_context("microsoft", "gid", "/x/y.exe", work_dir, "", app_id)
    assert ctx.steam_app_id is None
