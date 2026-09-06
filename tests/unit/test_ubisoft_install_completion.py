"""A Ubisoft game is installed only once UPC has drained its staging directory.

The reported bug: a Ubisoft install and a Battle.net install ran at the same
time, the second starting before the first had finished. The download queue was
not at fault — it is strictly serial, ``max_concurrent = 1`` across all stores.
It released the slot because the Ubisoft watcher told it the install was done.

Measured on the failing build (Tom Clancy's Splinter Cell, 2.4 GB)::

    05:01:54  starting install for ubisoft:0422d076-…
    05:04:19  detected install at …/Tom Clancy's Splinter Cell
    05:05:00  manual install complete: … (2428 MB)     <- 49 MB had landed
    05:05:01  starting install for battlenet:wlby      <- 1.2 s later
    05:23:23  UPC actually finished

Two things had to be true at once.

``UbisoftInstallProbe.is_complete`` returned ``None``, so the shared watcher
fell back to its size heuristic: three consecutive unchanged non-zero reads.
And ``measure`` returned *apparent* size, which UPC sets to the game's full
final length the instant it accepts the job by pre-allocating every file as a
sparse file. The figure was therefore final and perfectly stable within seconds
of the download starting, and stability tripped at the earliest poll the loop
allows — 41 s after detection, four polls, the floor.

The same premature completion ran finalisation against a still-staged tree, so
the recorded launch executable was ``uplay_download/109/…/register.exe``: a
0.9 MB installer helper inside the directory UPC empties when it finishes. That
path stopped existing at 05:23. Hence the last two cases here.
"""

from __future__ import annotations

from pathlib import Path

from unifideck.core.exe_finder import ExeFinder
from unifideck.stores.shared.installed_size import (
    dir_allocated_bytes,
    dir_size_bytes,
)
from unifideck.stores.ubisoft.installer.manual_ui_poll import UbisoftInstallProbe
from unifideck.stores.ubisoft.library.detection_helpers import (
    UPC_STAGING_DIR,
    find_game_executable,
)

SPARSE_LEN = 64 * 1024 * 1024
REAL_WRITE = 128 * 1024


def _preallocate(path: Path, length: int = SPARSE_LEN) -> None:
    """Reserve *length* bytes without committing them — what UPC does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.truncate(length)


def _commit(path: Path, nbytes: int = REAL_WRITE) -> None:
    """Actually write bytes into an already-reserved file."""
    with path.open("r+b") as fh:
        fh.write(b"\xa5" * nbytes)


def _game_dir(tmp_path: Path) -> Path:
    """A game folder as UPC lays it out, before anything is downloaded."""
    game = tmp_path / "games" / "Tom Clancy's Splinter Cell"
    game.mkdir(parents=True)
    return game


# --------------------------------------------------------------------------
# The measurement itself
# --------------------------------------------------------------------------

def test_apparent_size_does_not_move_when_real_bytes_land(tmp_path: Path) -> None:
    """The lie, reproduced: 64 MB reserved, 128 KB written, no visible change.

    This is the whole defect in three lines. Whatever the heuristic did with
    this number, it was never going to see a download in progress.
    """
    blob = tmp_path / "Sounds" / "STREAM.SS0"
    _preallocate(blob)
    apparent_before = dir_size_bytes(str(tmp_path))
    allocated_before = dir_allocated_bytes(str(tmp_path))

    _commit(blob)

    assert dir_size_bytes(str(tmp_path)) == apparent_before == SPARSE_LEN, (
        "apparent size was final before a single byte was committed"
    )
    assert dir_allocated_bytes(str(tmp_path)) > allocated_before, (
        "allocated size must track bytes actually landing"
    )


def test_allocated_size_is_far_below_apparent_mid_download(tmp_path: Path) -> None:
    """The incident's ratio: 2428 MB claimed against 49 MB committed."""
    for name in ("Sounds/MAPS.SM0", "Sounds/STREAM.SS0", "textures/T.utx"):
        _preallocate(tmp_path / name)
    _commit(tmp_path / "Sounds/MAPS.SM0")

    assert dir_size_bytes(str(tmp_path)) == 3 * SPARSE_LEN
    assert dir_allocated_bytes(str(tmp_path)) < dir_size_bytes(str(tmp_path)) // 10


def test_a_finished_install_measures_the_same_both_ways(tmp_path: Path) -> None:
    """No holes, no disagreement — why the display path keeps apparent size."""
    dense = tmp_path / "system" / "SplinterCell.exe"
    dense.parent.mkdir(parents=True)
    dense.write_bytes(b"\xc3" * REAL_WRITE)

    assert dir_allocated_bytes(str(tmp_path)) >= dir_size_bytes(str(tmp_path))


# --------------------------------------------------------------------------
# The completion verdict
# --------------------------------------------------------------------------

def test_no_staging_directory_yet_is_no_opinion(tmp_path: Path) -> None:
    """UPC has accepted the job but not staged: defer to the size fallback."""
    game = _game_dir(tmp_path)
    probe = UbisoftInstallProbe(str(tmp_path), str(tmp_path))

    assert probe.is_complete(str(game)) is None


def test_a_populated_staging_directory_is_not_complete(tmp_path: Path) -> None:
    """The arm that would have prevented the incident.

    ``False`` rather than ``None`` matters: the shared watcher only consults
    its size heuristic on ``None``, so this actively suppresses it.
    """
    game = _game_dir(tmp_path)
    _preallocate(game / UPC_STAGING_DIR / "109" / "Sounds" / "MAPS.SM0")
    probe = UbisoftInstallProbe(str(tmp_path), str(tmp_path))

    assert probe.is_complete(str(game)) is False


def test_drained_staging_with_a_real_executable_is_complete(tmp_path: Path) -> None:
    """UPC moved the game into place and emptied staging — the 05:23:23 state."""
    game = _game_dir(tmp_path)
    (game / UPC_STAGING_DIR).mkdir()
    (game / "system").mkdir()
    (game / "system" / "SplinterCell.exe").write_bytes(b"\xc3" * 1024)
    probe = UbisoftInstallProbe(str(tmp_path), str(tmp_path))

    assert probe.is_complete(str(game)) is True


def test_drained_staging_without_an_executable_is_no_opinion(tmp_path: Path) -> None:
    """Corroboration required: an empty staging dir alone does not mean done.

    UPC creates ``uplay_download/`` before it puts anything in it, so there is
    a window where the directory exists and is empty and nothing has been
    downloaded at all.
    """
    game = _game_dir(tmp_path)
    (game / UPC_STAGING_DIR).mkdir()
    probe = UbisoftInstallProbe(str(tmp_path), str(tmp_path))

    assert probe.is_complete(str(game)) is None


def test_an_executable_only_inside_staging_does_not_count(tmp_path: Path) -> None:
    """At 05:05 every ``.exe`` in the tree was under ``uplay_download/``."""
    game = _game_dir(tmp_path)
    staged_exe = game / UPC_STAGING_DIR / "109" / "system" / "UCC.exe"
    staged_exe.parent.mkdir(parents=True)
    staged_exe.write_bytes(b"\xc3" * 1024)
    probe = UbisoftInstallProbe(str(tmp_path), str(tmp_path))

    assert probe.is_complete(str(game)) is False, "staging is not empty"


def test_probe_measures_allocated_not_apparent(tmp_path: Path) -> None:
    """The probe's own ``measure`` must not reintroduce the lie."""
    game = _game_dir(tmp_path)
    _preallocate(game / UPC_STAGING_DIR / "109" / "Sounds" / "MAPS.SM0")
    probe = UbisoftInstallProbe(str(tmp_path), str(tmp_path))

    assert probe.measure(str(game)) < SPARSE_LEN


# --------------------------------------------------------------------------
# Never launch out of the staging directory
# --------------------------------------------------------------------------

def test_find_game_executable_ignores_staging(tmp_path: Path) -> None:
    """``register.exe`` is a clean basename; only its directory disqualifies it."""
    game = _game_dir(tmp_path)
    staged = game / UPC_STAGING_DIR / "109" / "Support" / "inst" / "Register"
    staged.mkdir(parents=True)
    (staged / "register.exe").write_bytes(b"\xc3" * (900 * 1024))
    real = game / "system"
    real.mkdir()
    (real / "SplinterCell.exe").write_bytes(b"\xc3" * 1024)

    found = find_game_executable(str(game))

    assert found is not None
    assert UPC_STAGING_DIR not in Path(found).parts
    assert Path(found).name == "SplinterCell.exe", (
        "the staged helper is larger and would otherwise win on size"
    )


def test_find_game_executable_finds_nothing_while_only_staged(tmp_path: Path) -> None:
    """Better to report no executable than one that is about to be deleted."""
    game = _game_dir(tmp_path)
    staged = game / UPC_STAGING_DIR / "109" / "system"
    staged.mkdir(parents=True)
    (staged / "UCC.exe").write_bytes(b"\xc3" * 1024)

    assert find_game_executable(str(game)) is None


def test_exe_finder_prunes_staging(tmp_path: Path) -> None:
    """The generic scorer independently picked ``uplay_download/109/system/UCC.exe``."""
    game = _game_dir(tmp_path)
    staged = game / UPC_STAGING_DIR / "109" / "system"
    staged.mkdir(parents=True)
    (staged / "UCC.exe").write_bytes(b"\xc3" * (5 * 1024 * 1024))

    assert ExeFinder().find(str(game)) is None


def test_exe_finder_still_picks_a_real_executable(tmp_path: Path) -> None:
    """Pruning staging must not prune the game."""
    game = _game_dir(tmp_path)
    staged = game / UPC_STAGING_DIR / "109" / "system"
    staged.mkdir(parents=True)
    (staged / "UCC.exe").write_bytes(b"\xc3" * (5 * 1024 * 1024))
    real = game / "system"
    real.mkdir()
    (real / "SplinterCell.exe").write_bytes(b"\xc3" * 1024)

    found = ExeFinder().find(str(game))

    assert found is not None
    assert Path(found).name == "SplinterCell.exe"
