"""Title-matched save-dir auto-detection — audit register item 47.

This heuristic existed twice, and the two copies guarded the empty title
differently: ``status.py`` checked the **sanitised** value, ``gog_strategy``
the **raw** one. Consolidating surfaced a live defect in the GOG copy, which
these tests pin.

Why it matters more than a wrong guess: the returned directory is what cloud
sync uploads and what a restore writes back over. Matching an arbitrary
folder does not merely fail to find saves — it carries an unrelated
directory to the store.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from unifideck.services.cloud_save.path_resolver import find_save_dir_by_title

SAVED_GAMES = ("users", "steamuser", "Saved Games")


@pytest.fixture()
def drive_c(tmp_path: Path) -> Path:
    (tmp_path / Path(*SAVED_GAMES)).mkdir(parents=True)
    return tmp_path


def _saved_games(drive_c: Path) -> Path:
    return drive_c / Path(*SAVED_GAMES)


# ── the defect the consolidation fixed ──────────────────────────────
@pytest.mark.parametrize("title", ["!!!", "「」", "—", "   ", "...", "东方"])
def test_a_title_with_no_ascii_alphanumerics_matches_nothing(
    drive_c: Path, title: str,
) -> None:
    """The GOG copy returned the first directory it found for these.

    Its guard was ``if not game_title`` — a non-empty title that sanitises
    to ``""`` sailed through, and ``"" in child_name`` is true for every
    child.
    """
    (_saved_games(drive_c) / "Some Unrelated Game").mkdir()

    assert find_save_dir_by_title(drive_c, title) is None


def test_a_punctuation_only_directory_does_not_match_every_title(
    drive_c: Path,
) -> None:
    """The mirror of the same bug, present in both copies.

    A child named entirely of punctuation sanitises to ``""``, and
    ``child_name in safe_title`` is then true for any title.
    """
    (_saved_games(drive_c) / "...").mkdir()

    assert find_save_dir_by_title(drive_c, "The Witcher 3") is None


def test_an_empty_title_matches_nothing(drive_c: Path) -> None:
    (_saved_games(drive_c) / "Anything").mkdir()

    assert find_save_dir_by_title(drive_c, "") is None


# ── the behaviour that must survive ─────────────────────────────────
def test_it_matches_ignoring_case_and_punctuation(drive_c: Path) -> None:
    target = _saved_games(drive_c) / "The Witcher 3"
    target.mkdir()

    assert find_save_dir_by_title(drive_c, "the-witcher-3!") == str(target)


def test_a_directory_name_contained_in_the_title_also_matches(
    drive_c: Path,
) -> None:
    """Matching is bidirectional: games abbreviate their own folder."""
    target = _saved_games(drive_c) / "Witcher3"
    target.mkdir()

    assert find_save_dir_by_title(drive_c, "The Witcher 3") == str(target)


def test_files_are_never_returned(drive_c: Path) -> None:
    (_saved_games(drive_c) / "Witcher3.sav").write_text("x")

    assert find_save_dir_by_title(drive_c, "Witcher3") is None


def test_the_roots_are_tried_in_order(tmp_path: Path) -> None:
    """``Saved Games`` wins over ``Documents`` when both hold a match."""
    for parts in (SAVED_GAMES, ("users", "steamuser", "Documents")):
        (tmp_path / Path(*parts) / "Portal2").mkdir(parents=True)

    assert find_save_dir_by_title(tmp_path, "Portal 2") == str(
        tmp_path / Path(*SAVED_GAMES) / "Portal2",
    )


def test_a_later_root_is_used_when_the_earlier_ones_are_empty(
    tmp_path: Path,
) -> None:
    roaming = tmp_path / "users" / "steamuser" / "AppData" / "Roaming"
    (roaming / "Portal2").mkdir(parents=True)

    assert find_save_dir_by_title(tmp_path, "Portal 2") == str(roaming / "Portal2")


def test_the_result_is_stable_across_calls(drive_c: Path) -> None:
    """``os.listdir`` order is arbitrary; a heuristic that picks "the first
    match" must not pick a different one on the next run."""
    for name in ("Portal 2", "Portal 2 Community", "portal2"):
        (_saved_games(drive_c) / name).mkdir()

    results = {find_save_dir_by_title(drive_c, "Portal 2") for _ in range(5)}

    assert len(results) == 1


# ── never raise ─────────────────────────────────────────────────────
def test_a_missing_prefix_is_none_not_an_error(tmp_path: Path) -> None:
    assert find_save_dir_by_title(tmp_path / "no-such-prefix", "Portal 2") is None
