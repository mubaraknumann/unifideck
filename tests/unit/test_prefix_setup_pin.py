"""Tests for ``prefix_setup._pin_final_tool`` — keeping the recovered Proton.

When ``setup_prefix`` recovers a hanging Proton by falling back to the managed
GE-Proton, the NEXT launch must resolve GE directly instead of re-picking the
hanging Proton, seeing a "Proton family change" against the GE-built prefix,
and wiping + rebuilding it at Play time (the observed Rise-of-the-Tomb-Raider
redo).

Two halves, both asserted here:
  1. re-stamp the prefix's ``.unifideck_proton_version`` marker (so
     ``ensure_prefix_initialized`` sees no family change next launch), and
  2. ask the frontend to make GE the game's Steam Compatibility setting, the
     one record of a game's Proton. Nothing goes to ``proton_settings.json``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from unifideck.launcher import frontend_bridge
from unifideck.launcher.proton import prefix_setup as setup_mod
from unifideck.launcher.proton.compat.prefix_init import _MARKER_NAME


@pytest.fixture
def _isolated_prefixes(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so nothing here can touch real user data."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def steam(monkeypatch):
    """Record Steam compat-tool requests; fail loudly on a plugin-side pin."""
    request = MagicMock(return_value=True)
    monkeypatch.setattr(frontend_bridge, "request_steam_compat_tool", request)
    monkeypatch.setattr(
        "unifideck.compatibility.proton_helpers.save_proton_setting",
        MagicMock(side_effect=AssertionError("no plugin-side Proton pin")),
    )
    return request


def _ctx(game_key="gog:123", steam_app_id="4184012976"):
    return SimpleNamespace(game_id="123", game_key=game_key, steam_app_id=steam_app_id)


def _state(prefix_path):
    """The launch state ``proton_prepare`` populates."""
    return SimpleNamespace(prefix_path=prefix_path)


def test_pin_writes_marker_and_sets_steams_setting(_isolated_prefixes, steam):
    prefix = _isolated_prefixes / ".local/share/unifideck/prefixes/123"

    setup_mod._pin_final_tool(_ctx(), _state(prefix), "GE-Proton11-1")

    assert (prefix / _MARKER_NAME).read_text(encoding="utf-8") == "GE-Proton11-1"
    steam.assert_called_once_with("4184012976", "GE-Proton11-1", "gog:123")


def test_pin_stamps_the_resolved_prefix_not_the_default_layout(_isolated_prefixes, steam):
    """A Ubisoft prefix under a user-picked base gets the marker, not a stray dir.

    Regression: the root used to be rebuilt as
    ``~/.local/share/unifideck/prefixes/<game_id>``, which is right for every
    store except Ubisoft — whose path comes from ``ubisoft_id_map.json``. That
    stamped ``prefixes/80``, a directory no launch opens, while the real prefix
    kept its stale marker and got wiped again on the next launch.
    """
    real = _isolated_prefixes / "Games/prefixes/ubisoft/80"
    real.mkdir(parents=True)

    setup_mod._pin_final_tool(_ctx("ubisoft:80"), _state(real), "GE-Proton11-3")

    assert (real / _MARKER_NAME).read_text(encoding="utf-8") == "GE-Proton11-3"
    assert not (_isolated_prefixes / ".local/share/unifideck/prefixes/80").exists()


def test_pin_normalizes_a_pfx_suffixed_prefix(_isolated_prefixes, steam):
    """``<root>/pfx`` stamps ``<root>`` — the marker lives at the prefix root."""
    root = _isolated_prefixes / "Games/prefixes/ubisoft/80"
    (root / "pfx").mkdir(parents=True)

    setup_mod._pin_final_tool(_ctx("ubisoft:80"), _state(root / "pfx"), "GE-Proton11-3")

    assert (root / _MARKER_NAME).read_text(encoding="utf-8") == "GE-Proton11-3"


def test_pin_still_sets_steam_when_prefix_unresolved(_isolated_prefixes, steam):
    setup_mod._pin_final_tool(_ctx(), _state(None), "GE-Proton11-1")

    steam.assert_called_once_with("4184012976", "GE-Proton11-1", "gog:123")
