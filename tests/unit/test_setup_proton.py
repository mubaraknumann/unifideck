"""One prefix, one Proton: which Proton a game's prefix is set up and run on.

A prefix set up by one Proton and then run by another keeps symlinks into the
first one's shared install, and the second Proton's Wine writes through them
(measured: Proton Experimental rewrote a DLL inside the shared GE-Proton, so
every GE game saw a damaged Proton and it was re-downloaded on every start).

The rules pinned here: a choice wins; a game nobody chose for runs on GE
(written into Steam's per-game setting) unless another Proton already owns
its prefix; Ubisoft and Battle.net never switch.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unifideck.launcher.proton import setup_proton
from unifideck.launcher.proton.compat.prefix_init import _MARKER_NAME
from unifideck.launcher.proton.infrastructure import selector

_EXPERIMENTAL = Path("/steam/steamapps/common/Proton - Experimental")
_GE_LATEST = Path("/compat/GE-Proton11-12")
_GE_OLD = Path("/compat/GE-Proton11-7")


@pytest.fixture
def tools(monkeypatch):
    """Installed tools by id, plus the managed latest GE."""
    installed = {
        "GE-Proton11-12": _GE_LATEST / "proton",
        "GE-Proton11-7": _GE_OLD / "proton",
    }
    monkeypatch.setattr(selector, "resolve_proton_path", installed.get)
    latest = {"value": (_GE_LATEST / "proton", "GE-Proton11-12")}
    monkeypatch.setattr(selector, "select_managed_ge_proton", lambda: latest["value"])
    return latest


def _ctx(store="ea", proton_tool=None):
    return SimpleNamespace(store=store, game_key=f"{store}:1", proton_tool=proton_tool, steam_app_id="4184012976")


def _prefix(tmp_path, *, set_up_by=None, created_by=None):
    root = tmp_path / "prefix"
    root.mkdir()
    if set_up_by is not None:
        (root / "config_info").write_text(f"11.0-100\n{set_up_by}/files/share/fonts/\n{set_up_by}/files/lib/\n")
    if created_by is not None:
        (root / _MARKER_NAME).write_text(created_by)
    return root


_DEFAULT = (_EXPERIMENTAL / "proton", "proton_experimental", "global-default")


# ── who set the prefix up ───────────────────────────────────────────

def test_config_info_names_the_proton_that_last_set_up_the_prefix(tmp_path):
    root = _prefix(tmp_path, set_up_by=_EXPERIMENTAL)
    assert setup_proton.prefix_set_up_by(root) == _EXPERIMENTAL


def test_no_config_info_means_never_set_up(tmp_path):
    assert setup_proton.prefix_set_up_by(_prefix(tmp_path)) is None


@pytest.mark.parametrize(("name", "ge"), [
    ("GE-Proton11-12", True), ("Proton-GE Latest", True),
    ("Proton - Experimental", False), ("proton_experimental", False), (None, False),
])
def test_is_ge_tool(name, ge):
    assert setup_proton.is_ge_tool(name) is ge


# ── a choice wins ───────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["steam", "saved", "unifideck"])
def test_a_choice_is_kept_even_with_no_prefix(tmp_path, tools, source):
    chosen = (_EXPERIMENTAL / "proton", "proton_experimental", source)

    assert setup_proton.choose(_ctx(), _prefix(tmp_path), chosen) == (*chosen[:2], False)


def test_a_fresh_install_uses_the_ge_it_was_installed_with(tmp_path, tools):
    result = setup_proton.choose(_ctx(proton_tool="GE-Proton11-12"), _prefix(tmp_path), _DEFAULT)

    assert result == (_GE_LATEST / "proton", "GE-Proton11-12", False)


@pytest.mark.parametrize("store", ["ubisoft", "battlenet"])
def test_wrapper_stores_never_switch(tmp_path, tools, store):
    assert setup_proton.choose(_ctx(store), _prefix(tmp_path), _DEFAULT)[2] is False


# ── nobody chose ────────────────────────────────────────────────────

def test_no_prefix_yet_runs_on_latest_ge_and_writes_it_to_steam(tmp_path, tools):
    result = setup_proton.choose(_ctx(), _prefix(tmp_path), _DEFAULT)

    assert result == (_GE_LATEST / "proton", "GE-Proton11-12", True)


def test_an_existing_game_keeps_the_proton_its_prefix_runs_on(tmp_path, tools):
    """The borrow-era case: GE created it, Experimental has run it since."""
    root = _prefix(tmp_path, set_up_by=_EXPERIMENTAL, created_by="GE-Proton11-12")

    assert setup_proton.choose(_ctx(), root, _DEFAULT) == (*_DEFAULT[:2], False)


def test_a_prefix_created_by_another_proton_is_not_moved_to_ge(tmp_path, tools):
    """Moving it would be a Proton-family change, which resets the prefix."""
    root = _prefix(tmp_path, set_up_by=_GE_LATEST, created_by="proton_experimental")

    assert setup_proton.choose(_ctx(), root, _DEFAULT) == (*_DEFAULT[:2], False)


def test_a_ge_prefix_stays_on_its_own_ge_build(tmp_path, tools):
    """A missed Steam write must not move the game to a different GE."""
    root = _prefix(tmp_path, set_up_by=_GE_OLD, created_by="GE-Proton11-7")

    assert setup_proton.choose(_ctx(), root, _DEFAULT) == (_GE_OLD / "proton", "GE-Proton11-7", True)


def test_a_ge_prefix_whose_build_is_gone_moves_to_latest_ge(tmp_path, tools):
    root = _prefix(tmp_path, set_up_by=Path("/compat/GE-Proton10-4"), created_by="GE-Proton10-4")

    assert setup_proton.choose(_ctx(), root, _DEFAULT)[1:] == ("GE-Proton11-12", True)


def test_no_ge_available_keeps_the_selected_proton(tmp_path, tools):
    """Offline with no GE installed, the default tier hands back Experimental."""
    tools["value"] = (_EXPERIMENTAL / "proton", "proton_experimental")

    assert setup_proton.choose(_ctx(), _prefix(tmp_path), _DEFAULT) == (*_DEFAULT[:2], False)
