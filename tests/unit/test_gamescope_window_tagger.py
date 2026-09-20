"""The window tagger must find its zipapp in a PACKAGED plugin, not just in git.

Without the tagger there is no visible failure and no crash. The game starts,
audio plays, and in Gaming Mode gamescope never focuses it — so the user sits
on Steam's loading screen with only *Abort*. That is the entire symptom, and it
came from one missing file.

**The bug these tests exist to prevent.** ``bin/umu/umu/umu_run.py`` is a
*symlink* to ``umu-run`` in the source tree, and the plugin packager does not
preserve symlinks: every built plugin ships ``umu-run`` alone. The lookup asked
only for ``umu_run.py``, so it succeeded in a checkout and failed on every real
install, logging one WARNING and continuing.

So the load-bearing case here is :meth:`TestFindUmuZipapp.test_finds_the_real
_zipapp_when_the_symlink_was_stripped` — a plugin root shaped like a *packaged*
install. A test built from the repo layout would have passed throughout the bug.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from unifideck.launcher.proton.infrastructure import gamescope_window_tagger as tagger


def _make_zipapp(path: Path) -> Path:
    """A file that is genuinely importable by ``sys.path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Xlib/__init__.py", "")
    return path


def _umu_dir(root: Path) -> Path:
    return root / "bin" / "umu" / "umu"


class TestFindUmuZipapp:
    def test_finds_the_real_zipapp_when_the_symlink_was_stripped(
        self, tmp_path: Path,
    ) -> None:
        """A packaged plugin: ``umu-run`` only, no ``umu_run.py``.

        This is the shape of every real install, and the shape the original
        lookup could not handle.
        """
        _make_zipapp(_umu_dir(tmp_path) / "umu-run")
        assert tagger._find_umu_zipapp(tmp_path) == _umu_dir(tmp_path) / "umu-run"

    def test_finds_the_symlink_name_when_that_is_all_there_is(
        self, tmp_path: Path,
    ) -> None:
        _make_zipapp(_umu_dir(tmp_path) / "umu_run.py")
        assert tagger._find_umu_zipapp(tmp_path) == _umu_dir(tmp_path) / "umu_run.py"

    def test_prefers_the_real_zipapp_over_the_symlink_name(
        self, tmp_path: Path,
    ) -> None:
        _make_zipapp(_umu_dir(tmp_path) / "umu-run")
        _make_zipapp(_umu_dir(tmp_path) / "umu_run.py")
        assert tagger._find_umu_zipapp(tmp_path) == _umu_dir(tmp_path) / "umu-run"

    def test_resolves_through_a_symlink(self, tmp_path: Path) -> None:
        """The source checkout's layout must keep working."""
        _make_zipapp(_umu_dir(tmp_path) / "umu-run")
        (_umu_dir(tmp_path) / "umu_run.py").symlink_to("umu-run")
        assert tagger._find_umu_zipapp(tmp_path) is not None

    def test_rejects_a_file_that_is_not_a_zip(self, tmp_path: Path) -> None:
        """Importing needs an archive; a same-named text file is not one."""
        umu = _umu_dir(tmp_path)
        umu.mkdir(parents=True)
        (umu / "umu-run").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        assert tagger._find_umu_zipapp(tmp_path) is None

    def test_returns_none_when_nothing_is_bundled(self, tmp_path: Path) -> None:
        assert tagger._find_umu_zipapp(tmp_path) is None

    def test_ignores_a_directory_with_the_right_name(self, tmp_path: Path) -> None:
        (_umu_dir(tmp_path) / "umu-run").mkdir(parents=True)
        assert tagger._find_umu_zipapp(tmp_path) is None


class TestAgainstThisCheckout:
    def test_the_bundled_zipapp_is_found_and_carries_xlib(self) -> None:
        """The repo really does ship an archive with Xlib in it.

        Guards the other half: the lookup can be perfect and still be useless
        if what it finds has no ``Xlib`` to import.
        """
        from tests.unit._repo_root import find_repo_file

        bundled = find_repo_file("bin/umu/umu/umu-run")
        assert bundled is not None, "bundled umu zipapp is missing"
        assert zipfile.is_zipfile(bundled)
        with zipfile.ZipFile(bundled) as archive:
            assert any(n.startswith("Xlib/") for n in archive.namelist())

    def test_plugin_root_points_at_a_plugin(self) -> None:
        """``parents[4]`` must still be the install root after any move.

        The lookup is only as good as the root it starts from, and that root
        is a hard-coded number of hops up from this module's location.
        """
        root = tagger._plugin_root()
        assert (root / "py_modules" / "unifideck").is_dir()


@pytest.mark.parametrize("name", tagger._ZIPAPP_NAMES)
def test_every_candidate_name_is_accepted(tmp_path: Path, name: str) -> None:
    _make_zipapp(_umu_dir(tmp_path) / name)
    assert tagger._find_umu_zipapp(tmp_path) is not None
