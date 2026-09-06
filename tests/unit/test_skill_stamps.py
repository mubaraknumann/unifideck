"""Check 14: every skill file carries a freshness stamp — register item 22.

A skill is what the next reader trusts *instead of* reading the tree, so one
that does not say when it was last checked cannot be judged at all. Two files
had no stamp when this was written while the roadmap claimed stamps existed on
"all skills".

The check deliberately tests **existence, not accuracy** — no machine can tell
whether prose still describes the code. Existence is the half that can be
enforced, and it was the half that was missing. These tests pin that boundary
so nobody later "improves" the check into something it cannot deliver.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_validator():
    """Import the gate script by path — ``scripts/`` is not a package."""
    path = REPO_ROOT / "scripts" / "validate_architecture.py"
    spec = importlib.util.spec_from_file_location("_va_skills", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_va_skills"] = mod
    spec.loader.exec_module(mod)
    return mod


va = _load_validator()


@pytest.fixture()
def skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(va, "SKILLS_DIR", root)
    return root


def _write(root: Path, name: str, body: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── the violation it exists to catch ────────────────────────────────
def test_an_unstamped_skill_is_reported(skills: Path) -> None:
    _write(skills, "thing/SKILL.md", "# Thing\n\nNo stamp anywhere.\n")

    assert va.find_unstamped_skills() == ["thing/SKILL.md"]


def test_an_unstamped_skill_fails_the_gate(skills: Path) -> None:
    _write(skills, "thing/SKILL.md", "# Thing\n")

    assert va.report_unstamped_skills() == 1


def test_a_stamped_skill_passes(skills: Path) -> None:
    _write(skills, "thing/SKILL.md", "# Thing\n\nLast verified: 2026-08-28 against v0.7.5.\n")

    assert va.find_unstamped_skills() == []
    assert va.report_unstamped_skills() == 0


def test_one_unstamped_file_among_several_is_still_caught(skills: Path) -> None:
    """The real failure mode: a companion file added beside a stamped SKILL.md.

    Both files that had no stamp were companions, not entry points.
    """
    _write(skills, "thing/SKILL.md", "Last verified: 2026-08-28 against v0.7.5.\n")
    _write(skills, "thing/playbook.md", "# Playbook\n")

    assert va.find_unstamped_skills() == ["thing/playbook.md"]


# ── what counts as a stamp ──────────────────────────────────────────
@pytest.mark.parametrize(
    "line",
    [
        "Last verified: 2026-08-28 against v0.7.5.",
        "Last verified: 2026-07-02 against v0.7.0 (commit c64dbe0).",
        "> Last verified: 2026-01-01",
        "Some prose. Last verified: 2026-08-28, mid-sentence.",
    ],
)
def test_a_date_anywhere_in_the_file_satisfies_it(skills: Path, line: str) -> None:
    """Placement is not prescribed — only that a date is claimed.

    An **old** date still passes, on purpose. A stale stamp is a signal to the
    reader, not a gate failure; a *missing* one leaves them nothing to judge.
    """
    _write(skills, "thing/SKILL.md", f"# Thing\n\n{line}\n")

    assert va.find_unstamped_skills() == []


@pytest.mark.parametrize(
    "line",
    ["Last verified: recently", "Last verified: v0.7.5", "Verified 2026-08-28"],
)
def test_a_stamp_without_an_iso_date_does_not_count(skills: Path, line: str) -> None:
    """"Recently" is what the stamp exists to replace."""
    _write(skills, "thing/SKILL.md", f"# Thing\n\n{line}\n")

    assert va.find_unstamped_skills() == ["thing/SKILL.md"]


# ── the CI boundary ─────────────────────────────────────────────────
def test_a_missing_skills_dir_passes_rather_than_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.claude/`` is gitignored, so CI never sees it.

    This is a **local** gate. Failing when the directory is absent would break
    every CI run for a condition CI cannot observe or fix.
    """
    monkeypatch.setattr(va, "SKILLS_DIR", tmp_path / "does-not-exist")

    assert va.find_unstamped_skills() == []
    assert va.report_unstamped_skills() == 0


def test_non_markdown_files_are_ignored(skills: Path) -> None:
    """Skills ship helper scripts; only the prose makes claims about the tree."""
    _write(skills, "thing/SKILL.md", "Last verified: 2026-08-28.\n")
    _write(skills, "thing/scripts/tracker.py", "# no stamp here\n")

    assert va.find_unstamped_skills() == []


# ── the live tree ───────────────────────────────────────────────────
def test_the_repos_own_skills_are_all_stamped() -> None:
    """Guards the fix itself on a machine that has the skills.

    Deliberately **not** a ``pytest.skip`` when ``.claude/skills/`` is absent:
    CI fails on any skip, and the directory is gitignored, so a skip here
    would fail every CI run. It needs no guard anyway — the finder returns
    ``[]`` for a missing directory, so this asserts the real thing locally
    and is vacuously true in CI.
    """
    assert va.find_unstamped_skills() == []
