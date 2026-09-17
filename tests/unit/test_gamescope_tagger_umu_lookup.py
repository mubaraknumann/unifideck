"""``_find_umu_zipapp`` must locate the umu zipapp under either name.

umu renamed its shipped executable from ``umu_run.py`` to ``umu-run``
(current bundles, e.g. umu 1.4.4). The tagger loads Xlib off that file to
set ``STEAM_GAME`` on game windows; when it isn't found, gamescope never
foregrounds the game and Gaming Mode shows audio but no picture. This is a
regression guard for that exact miss.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from unifideck.launcher.proton.infrastructure import gamescope_window_tagger


def _make_plugin_tree(tmp_path: Path, umu_filename: str) -> Path:
    """Build ``<root>/bin/umu/umu/<umu_filename>`` and return the file path."""
    umu_dir = tmp_path / "bin" / "umu" / "umu"
    umu_dir.mkdir(parents=True)
    zipapp = umu_dir / umu_filename
    zipapp.write_text("#!/usr/bin/env python3\n")  # shebang, contents irrelevant here
    return zipapp


def _patch_plugin_root(tmp_path: Path):
    """Point ``_find_umu_zipapp``'s ``__file__``-relative walk at ``tmp_path``.

    The function derives plugin_root as ``parents[4]`` of the module file, so
    place a fake module file 5 levels deep under tmp_path.
    """
    fake_module = (
        tmp_path
        / "py_modules" / "unifideck" / "launcher" / "proton" / "infrastructure"
        / "gamescope_window_tagger.py"
    )
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("")
    return mock.patch.object(
        gamescope_window_tagger, "__file__", str(fake_module),
    )


def test_finds_current_umu_run_name(tmp_path: Path) -> None:
    expected = _make_plugin_tree(tmp_path, "umu-run")
    with _patch_plugin_root(tmp_path):
        assert gamescope_window_tagger._find_umu_zipapp() == expected


def test_finds_legacy_umu_run_py_name(tmp_path: Path) -> None:
    expected = _make_plugin_tree(tmp_path, "umu_run.py")
    with _patch_plugin_root(tmp_path):
        assert gamescope_window_tagger._find_umu_zipapp() == expected


def test_returns_none_when_absent(tmp_path: Path) -> None:
    (tmp_path / "bin" / "umu" / "umu").mkdir(parents=True)
    with _patch_plugin_root(tmp_path):
        assert gamescope_window_tagger._find_umu_zipapp() is None
