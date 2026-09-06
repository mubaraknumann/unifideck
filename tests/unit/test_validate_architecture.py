"""Guard test — scripts/validate_architecture.py.

Audit §1.1.4's lesson applied to the guard itself: a gate nobody has watched
fail is indistinguishable from one that passes everything. Every check here
is exercised against a planted violation, not only against a clean tree.

Why this file exists at all is audit §2.1. The RPC mixin count was written
into prose in five places, went stale, was hand-corrected to a single agreed
figure, and went stale again in the same release when the §1.2 dead-RPC pass
deleted three emptied mixins. Correcting the number is the approach that
failed twice. Check 1 owns the one statement of the set that has to be right
(``main.py`` vs ``__all__``); check 5 stops a third statement appearing
anywhere else.

Checks 6, 7 and 8 arrived with the §2.2, §2.4 and §2.5 passes and share check
5's scanner. Their false-positive tests are the point of this file, not
padding: a first cut of check 6 fired on seven correct ``3-layer merge`` lines
in ``config/``, and a first cut of check 7 reported 23 correct subset
statements ("Amazon is the one store whose…") in a single run. Both are
parametrised below with the verbatim lines, because a gate that reds untouched
code gets switched off rather than fixed.

What is pinned:

1. the real repo passes every check, each check still prints its own line
   (one that stops printing has stopped running), and the live mixin count is
   printed so the set cannot change silently;
2. check 1 flips the exit code and names the offending class in BOTH
   directions — composed-but-not-exported and exported-but-not-composed —
   because the fix differs per direction;
3. check 5 catches a count written next to the word, names file, line and
   the live figure, and honours its ``mixin-count-ok:`` opt-out on the line
   and on the line above;
4. check 5's exclusions and its lookbehind hold, since a checker that fires
   on ``Layer-6 RPC mixins`` gets switched off rather than fixed;
5. check 6 catches every historical layer-count phrasing, including the two
   workflow comments the audit's own list missed, and leaves both the config
   merge and ordinal ``Layer N`` references alone. Its marker reach is pinned
   at one line, so widening it fails a test;
6. check 7 verifies rather than bans: a stale total fails, a correct total
   passes, subset statements are untouched, and the same figure fails once
   the store count moves under it;
7. check 8 names a subpackage absent from the layer map, skips ``__init__``
   and ``__pycache__``, and stays silent when the doc itself is missing;
8. check 9 reads the real ``CLIENT_STOREFRONTS`` declaration shape — whose
   generic parameter contains ``=>`` and broke a first cut — catches a
   dropped wrapper store, and raises rather than reporting an empty set when
   the map is renamed away.

The clean-tree case runs the script as a subprocess against the real repo.
The failure cases build a throwaway mirror of the repo — symlinks for every
directory, a rewritten ``main.py`` — and repoint the module's roots at it,
so no test ever mutates the working tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _find_script() -> Path | None:
    from tests.unit._repo_root import find_repo_file

    return find_repo_file("scripts/validate_architecture.py")


@pytest.fixture(scope="module")
def script_path() -> Path:
    p = _find_script()
    if p is None:
        pytest.skip(
            "scripts/validate_architecture.py not found "
            "(set UNIFIDECK_REPO_ROOT to the checkout root)")
    return p


@pytest.fixture(scope="module")
def repo_root() -> Path:
    from tests.unit._repo_root import find_repo_root

    root = find_repo_root()
    if root is None:
        pytest.skip(
            "repo checkout not found "
            "(set UNIFIDECK_REPO_ROOT to the checkout root)")
    return root


def _load(script: Path):
    """Fresh module instance so a test's root patch cannot leak."""
    spec = importlib.util.spec_from_file_location("_va_under_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(script_path: Path):
    return _load(script_path)


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=180, check=False,
    )


# Directories the script reads. Symlinked rather than copied: the real
# stores/, src/ and docs/ must be the ones under test, so that only the
# planted defect distinguishes the mirror from the checkout.
_MIRRORED = (
    "py_modules", "src", "docs", "scripts", ".github", ".claude", "CLAUDE.md",
)


def _mirror(tmp_path: Path, repo_root: Path, main_py: str) -> Path:
    """A repo whose only difference from the real one is ``main.py``."""
    root = tmp_path / "mirror"
    root.mkdir()
    for entry in _MIRRORED:
        source = repo_root / entry
        if source.exists():
            (root / entry).symlink_to(
                source, target_is_directory=source.is_dir())
    (root / "main.py").write_text(main_py, encoding="utf-8")
    return root


def _repoint(mod, root: Path) -> None:
    mod.REPO_ROOT = root
    mod.PY = root / "py_modules" / "unifideck"
    mod.SRC = root / "src"


# ========================================================= #
# 1. Clean run against the real source
# ========================================================= #
def test_passes_against_real_source(script_path: Path) -> None:
    res = _run(script_path)
    assert res.returncode == 0, (
        "architecture gate failed against real source:\n"
        f"{res.stdout}\n{res.stderr}")
    assert "architecture invariants OK" in res.stdout


def test_every_check_reports(script_path: Path) -> None:
    """A check that stops printing has stopped running."""
    res = _run(script_path)
    for expected in (
        "mixins composed == __all__",
        "stores agree (cache registry == disk)",
        "StoreInfo.name matches its directory",
        "have a frontend caller",
        "no mixin count restated in prose",
        "no layer count restated in prose",
        "every prose store count agrees",
        "module is documented",
        "CLIENT_STOREFRONTS covers all",
        "vendor log globs salvages them",
        "shared helpers defined once",
    ):
        assert expected in res.stdout, f"no output line for: {expected}"


def test_the_live_mixin_count_is_printed(
    script_path: Path, repo_root: Path, mod,
) -> None:
    """The count is printed, and it is the count main.py actually composes.

    Derived from the tree rather than written down here: a literal in this
    test would be the sixth stale copy of the number §2.1 is about.
    """
    composed = mod.parse_mixin_bases(repo_root / "main.py")
    res = _run(script_path)
    assert f"OK: {len(composed)} mixins composed == __all__" in res.stdout


def test_mirror_alone_does_not_change_the_verdict(
    tmp_path: Path, repo_root: Path, mod, capsys,
) -> None:
    """Pins the harness, not the script.

    Every failure case below is a mirror plus one planted defect. If an
    unmodified mirror did not pass, those tests would prove nothing about
    the defect they plant.
    """
    root = _mirror(
        tmp_path, repo_root, (repo_root / "main.py").read_text())
    _repoint(mod, root)
    assert mod.main() == 0, capsys.readouterr().out


# ========================================================= #
# 2. Check 1 — main.py vs __all__, both directions
# ========================================================= #
def _main_py_dropping(repo_root: Path, mixin: str) -> str:
    text = (repo_root / "main.py").read_text()
    needle = f"    {mixin},\n"
    assert needle in text, f"{mixin} is not a base of class Plugin(...)"
    return text.replace(needle, "")


def test_check1_catches_a_mixin_left_in_all_but_not_composed(
    tmp_path: Path, repo_root: Path, mod, capsys,
) -> None:
    """Deleting a mixin from main.py and forgetting __all__.

    This is the live half: the §1.2 pass deleted three mixins, and this is
    the check that made that safe.
    """
    root = _mirror(
        tmp_path, repo_root, _main_py_dropping(repo_root, "AchievementsRPCMixin"))
    _repoint(mod, root)

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "mixin set drift" in out
    assert "in __all__ but not composed: ['AchievementsRPCMixin']" in out


def test_check1_catches_a_mixin_composed_but_not_exported(
    tmp_path: Path, repo_root: Path, mod, capsys,
) -> None:
    """Adding a mixin to main.py and forgetting __all__ — the other half.

    Named separately because the remedy differs: here __all__ needs the new
    import, there main.py needs the base removed.
    """
    text = (repo_root / "main.py").read_text()
    planted = text.replace(
        "    UpdaterRPCMixin,\n",
        "    UpdaterRPCMixin,\n    NewlyAddedRPCMixin,\n",
        1,
    )
    assert planted != text
    _repoint(mod, _mirror(tmp_path, repo_root, planted))

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "missing from __all__: ['NewlyAddedRPCMixin']" in out


def test_check1_reports_the_two_figures_it_compared(
    tmp_path: Path, repo_root: Path, mod, capsys,
) -> None:
    """"They disagree" is not a usable diagnosis without both numbers."""
    composed = mod.parse_mixin_bases(repo_root / "main.py")
    root = _mirror(
        tmp_path, repo_root, _main_py_dropping(repo_root, "EdgeRPCMixin"))
    _repoint(mod, root)

    mod.main()
    out = capsys.readouterr().out
    assert f"main.py composes {len(composed) - 1} mixins" in out
    assert f"__all__ re-exports {len(composed)}" in out


def test_parse_mixin_bases_ignores_a_commented_out_base(
    tmp_path: Path, mod,
) -> None:
    """A commented-out base must read as absent, or check 1 sees no drift."""
    src = tmp_path / "main.py"
    src.write_text(
        "class Plugin(\n"
        "    AlphaRPCMixin,\n"
        "    # BetaRPCMixin,\n"
        "    GammaRPCMixin,\n"
        "):\n"
        "    pass\n",
        encoding="utf-8",
    )
    assert mod.parse_mixin_bases(src) == {"AlphaRPCMixin", "GammaRPCMixin"}


# ========================================================= #
# 3. Check 5 — no mixin count restated in prose
# ========================================================= #
def _doc(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_check5_catches_a_count_next_to_the_word(tmp_path: Path, mod) -> None:
    _doc(tmp_path, "docs/architecture.md",
         "intro\nThe Plugin class is composed from 20 RPC mixin classes.\n")

    hits = mod.find_prose_mixin_counts(tmp_path)
    assert [(rel, line) for rel, line, _ in hits] == [
        ("docs/architecture.md", 2)]
    assert "20 RPC mixin" in hits[0][2]


@pytest.mark.parametrize("phrasing", [
    "composed from 20 RPC mixin classes",
    "Plugin = 20 RPC mixins (@auto_wrap_rpc_methods)",
    "class Plugin(<20 RPC mixins>)",
    'the docstring says "eleven mixins"',
    "which are the 20 mixin surfaces",
    "re-exports seventeen mixins",
])
def test_check5_catches_every_form_the_defect_actually_took(
    tmp_path: Path, mod, phrasing: str,
) -> None:
    """The parametrised strings are the real §2.1 sites, verbatim."""
    _doc(tmp_path, "docs/architecture.md", phrasing + "\n")
    assert mod.find_prose_mixin_counts(tmp_path), (
        f"not caught: {phrasing!r}")


def test_check5_honours_the_marker_on_the_line(tmp_path: Path, mod) -> None:
    _doc(tmp_path, "docs/engineering-roadmap.md",
         "<!-- mixin-count-ok: historical -->  It said 20 RPC mixins.\n")
    assert mod.find_prose_mixin_counts(tmp_path) == []


def test_check5_honours_the_marker_on_the_line_above(
    tmp_path: Path, mod,
) -> None:
    _doc(tmp_path, "docs/engineering-roadmap.md",
         "<!-- mixin-count-ok: historical -->\nIt said 20 RPC mixins.\n")
    assert mod.find_prose_mixin_counts(tmp_path) == []


def test_check5_marker_does_not_exempt_the_line_below_it(
    tmp_path: Path, mod,
) -> None:
    """Two lines of reach, not three — an unbounded marker is an allowlist."""
    _doc(tmp_path, "docs/architecture.md",
         "It said 20 RPC mixins.\nfiller\n<!-- mixin-count-ok: x -->\n")
    assert len(mod.find_prose_mixin_counts(tmp_path)) == 1


def test_check5_reports_both_counts_on_one_line(tmp_path: Path, mod) -> None:
    """Fixing one of two must not need a second run to see the other."""
    _doc(tmp_path, "docs/architecture.md",
         "It said eleven mixins, then 20 RPC mixins.\n")
    assert len(mod.find_prose_mixin_counts(tmp_path)) == 2


def test_check5_does_not_fire_on_a_layer_number(tmp_path: Path, mod) -> None:
    """``Layer-6 RPC mixins`` is live prose in services/__init__.py.

    A word boundary sits between the hyphen and the digit, so without the
    lookbehind this reads as a count of six and the gate cries wolf on
    untouched code — which is how a checker gets switched off.
    """
    _doc(tmp_path, "docs/architecture.md",
         "the services layer sits below the Layer-6 RPC mixins\n")
    assert mod.find_prose_mixin_counts(tmp_path) == []


def test_check5_does_not_fire_mid_word(tmp_path: Path, mod) -> None:
    _doc(tmp_path, "docs/architecture.md", "someone mixins things up\n")
    assert mod.find_prose_mixin_counts(tmp_path) == []


def test_check5_does_not_fire_on_a_countless_mention(
    tmp_path: Path, mod,
) -> None:
    """The wording every fixed site now uses must stay clean."""
    _doc(tmp_path, "docs/architecture.md",
         "composed from the RPC mixin classes enumerated in the table below\n")
    assert mod.find_prose_mixin_counts(tmp_path) == []


@pytest.mark.parametrize("excluded", [
    "docs/archive/ARCHITECTURE_TREE.md",
    "docs/architecture-audit.md",
])
def test_check5_skips_the_historical_records(
    tmp_path: Path, mod, excluded: str,
) -> None:
    """Superseded docs and the audit register exist to hold the old figures.

    Scanning them would produce noise the only fix for is an exemption on
    every line, which trains people to add exemptions.
    """
    _doc(tmp_path, excluded, "Thin Plugin router — 11 RPC mixins\n")
    assert mod.find_prose_mixin_counts(tmp_path) == []


def test_check5_scans_the_agent_facing_surfaces(tmp_path: Path, mod) -> None:
    """The stale count that mattered lived in a skill, not in docs/.

    SKILL.md is loaded as context for every architecture task, so leaving it
    out of scope would miss the highest-consequence copy.
    """
    for relative in (
        "CLAUDE.md",
        "docs/architecture.md",
        ".claude/skills/unifideck-architecture/SKILL.md",
        "main.py",
        "py_modules/unifideck/rpc/mixins/__init__.py",
        "scripts/validate_architecture.py",
        ".github/workflows/tests.yml",
    ):
        root = tmp_path / relative.replace("/", "_")
        root.mkdir()
        _doc(root, relative, "composed of 20 RPC mixins\n")
        assert mod.find_prose_mixin_counts(root), f"not scanned: {relative}"


def test_check5_failure_names_the_file_line_and_live_count(
    tmp_path: Path, repo_root: Path, mod, capsys,
) -> None:
    """End to end: the error has to say where, what, and what is true.

    Driven through ``main()`` rather than the helper so the exit code and
    the remediation block are pinned too.
    """
    root = _mirror(
        tmp_path, repo_root, (repo_root / "main.py").read_text())
    composed = mod.parse_mixin_bases(repo_root / "main.py")

    # docs/ is a symlink to the real tree; shadow one file with a real
    # directory so the planted count cannot touch the checkout.
    (root / "docs").unlink()
    _doc(root, "docs/architecture.md", "composed from 20 RPC mixin classes\n")
    _repoint(mod, root)

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "docs/architecture.md:1" in out
    assert "'20 RPC mixin'" in out
    assert f"main.py composes {len(composed)}" in out
    assert "mixin-count-ok: <reason>" in out


# ========================================================= #
# 6. Check 6 — the layer count, banned in prose
# ========================================================= #
@pytest.mark.parametrize("phrasing", [
    "The 5-layer backend, EventBus, RPC mixins, and build flow.",
    "Python, Decky Loader RPC, a 5-layer architecture with an EventBus",
    "The full 5-layer stack doesn't cleanly map to the current tree",
    "Architecture role : Layer 3 of the plan's five-layer model",
    "aligned with the five-layer architecture of the technical document",
    "The full 5-layer stack is documented in the technical doc",
])
def test_check6_catches_every_form_the_defect_actually_took(
    tmp_path: Path, mod, phrasing: str,
) -> None:
    """The parametrised strings are the real §2.2 sites, verbatim.

    Two of them (the two workflow comments) were not in the audit's own
    list; check 6 is how they were found.
    """
    _doc(tmp_path, "docs/architecture.md", phrasing + "\n")
    assert mod.find_prose_layer_counts(tmp_path), f"not caught: {phrasing!r}"


@pytest.mark.parametrize("phrasing", [
    "ConfigManager : 3-layer runtime config (defaults, user, code)",
    "Catches the case where a new call site skips the 3-layer merge",
    '"""3-layer configuration manager."""',
    "Initialize with 3-layer merge.",
    "``ConfigManager`` with 3-layer merge (defaults + user + code)",
])
def test_check6_leaves_the_config_merge_alone(
    tmp_path: Path, mod, phrasing: str,
) -> None:
    """The config merge really is 3-layer and has nothing to do with the stack.

    These are verbatim ``config/`` and ``bootstrap/`` lines. A first version
    of check 6 banned any cardinal before "layer" and fired on all of them.
    A gate that reds seven correct lines gets switched off rather than fixed,
    so the trailing architecture noun is load-bearing.
    """
    _doc(tmp_path, "py_modules/unifideck/config/config_manager.py",
         phrasing + "\n")
    assert mod.find_prose_layer_counts(tmp_path) == [], (
        f"false positive: {phrasing!r}")


@pytest.mark.parametrize("phrasing", [
    "Layer 3 — StoreBase (stores/shared/)",
    "The services layer sits between the Layer-4 stores",
    "Layer-6 RPC mixins + main.py",
    "### Layer 2 — `core/`",
])
def test_check6_does_not_read_an_ordinal_as_a_count(
    tmp_path: Path, mod, phrasing: str,
) -> None:
    """``Layer 3`` names one layer; only a count *before* the word is a total."""
    _doc(tmp_path, "docs/architecture.md", phrasing + "\n")
    assert mod.find_prose_layer_counts(tmp_path) == [], (
        f"false positive: {phrasing!r}")


def test_check6_honours_the_marker_on_the_line(tmp_path: Path, mod) -> None:
    _doc(tmp_path, "docs/engineering-roadmap.md",
         "<!-- layer-count-ok: historical --> It said 5-layer backend.\n")
    assert mod.find_prose_layer_counts(tmp_path) == []


def test_check6_honours_the_marker_on_the_line_above(
    tmp_path: Path, mod,
) -> None:
    _doc(tmp_path, "docs/engineering-roadmap.md",
         "<!-- layer-count-ok: historical -->\nIt said 5-layer backend.\n")
    assert mod.find_prose_layer_counts(tmp_path) == []


def test_check6_marker_does_not_reach_two_lines_down(
    tmp_path: Path, mod,
) -> None:
    """Pins the reach. Widening it silently excuses the line after the next."""
    _doc(tmp_path, "docs/engineering-roadmap.md",
         "<!-- layer-count-ok: historical -->\nfiller\n5-layer backend\n")
    assert len(mod.find_prose_layer_counts(tmp_path)) == 1


def test_check6_failure_names_the_file_and_line(
    tmp_path: Path, repo_root: Path, mod, capsys,
) -> None:
    root = _mirror(tmp_path, repo_root, (repo_root / "main.py").read_text())
    (root / "docs").unlink()
    _doc(root, "docs/architecture.md", "a 5-layer architecture with a bus\n")
    _repoint(mod, root)

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "docs/architecture.md:1" in out
    assert "5-layer architecture" in out
    assert "layer-count-ok: <reason>" in out


# ========================================================= #
# 7. Check 7 — the store count, verified rather than banned
# ========================================================= #
@pytest.mark.parametrize("phrasing", [
    "The five store connectors implement the same contract",
    "all five stores authenticate the same way",
    "a five-store system with per-store auth",
    "a five-store setup",
])
def test_check7_catches_a_stale_total(
    tmp_path: Path, mod, phrasing: str,
) -> None:
    """§2.4: every doc said five for a release after Battle.net landed."""
    _doc(tmp_path, "docs/architecture.md", phrasing + "\n")
    assert mod.find_wrong_store_counts(tmp_path, 6), f"not caught: {phrasing!r}"


@pytest.mark.parametrize("phrasing", [
    "the sole dispatcher for all six stores' DOWNLOAD_* events",
    "Six store connector sub-packages, each self-contained",
    "6 store connectors",
    "flows across all 6 stores:",
])
def test_check7_passes_a_correct_total(
    tmp_path: Path, mod, phrasing: str,
) -> None:
    """A right figure is not a defect. This is why check 7 verifies."""
    _doc(tmp_path, "docs/architecture.md", phrasing + "\n")
    assert mod.find_wrong_store_counts(tmp_path, 6) == [], (
        f"false positive: {phrasing!r}")


@pytest.mark.parametrize("phrasing", [
    "Amazon is the one store whose sign-in leaves the shared Edge profile",
    "Battle.net is the one store that reaches this module",
    "four stores report credential permissions through one channel",
    "Default SD / removable-media install base for one store.",
    "Two stores need this path:",
    "sync bar reported success for the other five stores.",
    "Tile-patch infrastructure from 0.7 store badges makes this cheap",
    "Four of Unifideck's six stores (Epic, GOG, Amazon, Microsoft)",
])
def test_check7_leaves_subset_statements_alone(
    tmp_path: Path, mod, phrasing: str,
) -> None:
    """Verbatim lines from the tree, all correct, none a total.

    A first version of check 7 matched any cardinal before "store" and
    reported 23 of these in one run. Only a total claim is checked, because
    a count below the total is nearly always naming a subset.
    """
    _doc(tmp_path, "py_modules/unifideck/stores/shared/install_base.py",
         phrasing + "\n")
    assert mod.find_wrong_store_counts(tmp_path, 6) == [], (
        f"false positive: {phrasing!r}")


def test_check7_catches_the_count_going_stale_upward(
    tmp_path: Path, mod,
) -> None:
    """The direction that matters next: a correct six once a seventh lands."""
    _doc(tmp_path, "docs/architecture.md", "all six stores are wired\n")
    assert mod.find_wrong_store_counts(tmp_path, 6) == []
    assert mod.find_wrong_store_counts(tmp_path, 7)


def test_check7_honours_the_marker(tmp_path: Path, mod) -> None:
    _doc(tmp_path, "docs/engineering-roadmap.md",
         "<!-- store-count-ok: historical --> It said five store connectors.\n")
    assert mod.find_wrong_store_counts(tmp_path, 6) == []


def test_check7_reports_both_the_stated_and_the_real_count(
    tmp_path: Path, repo_root: Path, mod, capsys,
) -> None:
    root = _mirror(tmp_path, repo_root, (repo_root / "main.py").read_text())
    (root / "docs").unlink()
    _doc(root, "docs/architecture.md", "The five store connectors share it\n")
    _repoint(mod, root)

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "docs/architecture.md:1" in out
    assert "says 5" in out
    # Derived, not written down: this assertion is about the message naming
    # BOTH numbers, and hardcoding the real one means every new store breaks
    # this test instead of the thing it guards. It said "6" until GameVault
    # became the seventh.
    real = len(mod.parse_store_caches(
        repo_root / "py_modules/unifideck/bootstrap/cache_registry.py",
    ))
    assert f"the tree has {real}" in out
    assert "store-count-ok: <reason>" in out


# ========================================================= #
# 8. Check 8 — every subpackage appears in the layer map
# ========================================================= #
def _fake_tree(root: Path, *, services=(), core=(), event_bus=()) -> None:
    base = root / "py_modules" / "unifideck"
    for name in services:
        (base / "services" / name).mkdir(parents=True, exist_ok=True)
    for package, modules in (("core", core), ("event_bus", event_bus)):
        directory = base / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
        for name in modules:
            (directory / name).write_text("", encoding="utf-8")


def test_check8_catches_an_undocumented_service_package(
    tmp_path: Path, mod,
) -> None:
    _fake_tree(tmp_path, services=("download", "support_bundle"))
    _doc(tmp_path, "docs/architecture.md", "| `services/download/` | queue |\n")

    missing = mod.find_undocumented_subpackages(
        tmp_path, tmp_path / "docs" / "architecture.md")
    assert missing == ["services/support_bundle"]


def test_check8_catches_an_undocumented_core_module(
    tmp_path: Path, mod,
) -> None:
    _fake_tree(tmp_path, core=("paths.py", "marker_sweep.py"))
    _doc(tmp_path, "docs/architecture.md", "| `paths.py` | path resolution |\n")

    missing = mod.find_undocumented_subpackages(
        tmp_path, tmp_path / "docs" / "architecture.md")
    assert missing == ["core/marker_sweep.py"]


def test_check8_ignores_dunder_init_and_pycache(tmp_path: Path, mod) -> None:
    """``__init__.py`` documents nothing, and ``__pycache__`` is not a package."""
    _fake_tree(tmp_path, services=("__pycache__",), core=("paths.py",))
    _doc(tmp_path, "docs/architecture.md", "`paths.py`\n")
    assert mod.find_undocumented_subpackages(
        tmp_path, tmp_path / "docs" / "architecture.md") == []


def test_check8_passes_when_everything_is_documented(
    tmp_path: Path, mod,
) -> None:
    _fake_tree(
        tmp_path, services=("artwork",), core=("paths.py",),
        event_bus=("event_bus.py",))
    _doc(tmp_path, "docs/architecture.md",
         "`artwork/` `paths.py` `event_bus.py`\n")
    assert mod.find_undocumented_subpackages(
        tmp_path, tmp_path / "docs" / "architecture.md") == []


def test_check8_is_silent_when_the_doc_is_missing(tmp_path: Path, mod) -> None:
    """No doc means a broken checkout, not 44 violations to wade through."""
    _fake_tree(tmp_path, services=("artwork",))
    assert mod.find_undocumented_subpackages(
        tmp_path, tmp_path / "docs" / "nope.md") == []


# ========================================================= #
# 9. Check 9 — the frontend wrapper-storefront map
# ========================================================= #
# Planted into a standalone .ts file rather than the mirror: ``_mirror``
# symlinks ``src/`` whole, so the real StorefrontLauncher.ts is deliberately
# the one under test everywhere else. ``parse_client_storefronts`` takes its
# path, so a fixture file exercises the parser directly.

_REAL_SHAPE = """\
const BROWSER_STOREFRONTS: Partial<
  Record<StoreId, () => Promise<ShortcutLaunchResult>>
> = {
  epic: launchEpicStorefrontViaShortcut,
  gog: launchGogStorefrontViaShortcut,
};

const CLIENT_STOREFRONTS: Partial<
  Record<StoreId, () => Promise<ShortcutLaunchResult>>
> = {
  ubisoft: () =>
    launchWrapperAuthViaShortcut(UBISOFT_SHORTCUT_CONFIG, "storefront"),
  battlenet: () =>
    launchWrapperAuthViaShortcut(BATTLENET_SHORTCUT_CONFIG, "storefront"),
};
"""


def _ts(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "StorefrontLauncher.ts"
    p.write_text(body, encoding="utf-8")
    return p


def test_check9_reads_the_real_declaration_shape(tmp_path: Path, mod) -> None:
    """The generic type parameter contains ``=>``, which broke a first cut.

    A ``[^=]*=`` scan stops inside ``() => Promise<...>`` and never reaches
    the assignment, so the parser reported the map as missing on a file that
    had it. Pinned because that failure mode is invisible in a green run.
    """
    assert mod.parse_client_storefronts(
        _ts(tmp_path, _REAL_SHAPE)) == {"ubisoft", "battlenet"}


def test_check9_does_not_pick_up_the_browser_map(tmp_path: Path, mod) -> None:
    """``BROWSER_STOREFRONTS`` is declared first and has the same shape."""
    keys = mod.parse_client_storefronts(_ts(tmp_path, _REAL_SHAPE))
    assert "epic" not in keys and "gog" not in keys


def test_check9_catches_a_dropped_wrapper_store(tmp_path: Path, mod) -> None:
    """The defect: a wrapper store whose cart button silently does nothing."""
    dropped = _REAL_SHAPE.replace(
        '  battlenet: () =>\n'
        '    launchWrapperAuthViaShortcut(BATTLENET_SHORTCUT_CONFIG, "storefront"),\n',
        "",
    )
    assert mod.parse_client_storefronts(_ts(tmp_path, dropped)) == {"ubisoft"}


def test_check9_ignores_call_arguments_inside_an_arrow_body(
    tmp_path: Path, mod,
) -> None:
    """Values are indented four spaces; only two-space keys are read.

    Without the indent anchor an object literal passed as an argument would
    contribute phantom store ids.
    """
    noisy = _REAL_SHAPE.replace(
        '    launchWrapperAuthViaShortcut(UBISOFT_SHORTCUT_CONFIG, "storefront"),',
        '    launchWrapperAuthViaShortcut(UBISOFT_SHORTCUT_CONFIG, {\n'
        '      mode: "storefront",\n'
        '    }),',
    )
    assert mod.parse_client_storefronts(_ts(tmp_path, noisy)) == {
        "ubisoft", "battlenet"}


def test_check9_refuses_rather_than_passes_when_the_map_is_gone(
    tmp_path: Path, mod,
) -> None:
    """A renamed or deleted map must fail loudly, not report an empty set.

    An empty set would compare unequal to WRAPPER_STORES and still fail, but
    with a message blaming the store list instead of the missing declaration.
    """
    with pytest.raises(SystemExit):
        mod.parse_client_storefronts(_ts(tmp_path, "const OTHER = {};\n"))


# ── check 10: vendor-log globs without a salvage call ──────────────────────
#
# The defect class this closes is the audit's most repeated one: material
# written, measured and shipped, with the call site never built. A grep for
# ``VENDOR_LOG_GLOBS`` found a complete Ubisoft row and read as covered,
# while every failed Ubisoft install deleted UPC's own logs with the prefix.


def test_check10_reads_the_store_keys_not_the_globs(mod) -> None:
    """Keys are indented four spaces, glob strings eight.

    Without the indent anchor the globs themselves would be read as store
    names and the check would compare nonsense against the tree.
    """
    stores = mod.parse_vendor_log_stores()
    assert stores == {"battlenet", "ubisoft"}


def test_check10_passes_on_the_real_tree(mod) -> None:
    """Both stores with globs actually call ``preserve_vendor_logs``."""
    assert mod.find_unsalvaged_vendor_logs() == set()


def test_check10_catches_a_store_that_never_salvages(
    monkeypatch, tmp_path: Path, mod,
) -> None:
    """The planted violation: globs declared, call site absent."""
    store = tmp_path / "stores" / "ghoststore"
    store.mkdir(parents=True)
    (store / "install.py").write_text(
        "async def cleanup():\n    pass\n", encoding="utf-8",
    )
    monkeypatch.setattr(mod, "PY", tmp_path)
    monkeypatch.setattr(mod, "parse_vendor_log_stores", lambda: {"ghoststore"})

    assert mod.find_unsalvaged_vendor_logs() == {"ghoststore"}


def test_check10_will_not_let_one_store_vouch_for_another(
    monkeypatch, tmp_path: Path, mod,
) -> None:
    """The call must live in the store's OWN package.

    Battle.net calling ``preserve_vendor_logs`` is precisely what made
    Ubisoft's missing call invisible to a whole-tree grep.
    """
    (tmp_path / "stores" / "battlenet").mkdir(parents=True)
    (tmp_path / "stores" / "battlenet" / "install.py").write_text(
        "await preserve_vendor_logs(STORE_ID, prefix, dest)\n", encoding="utf-8",
    )
    (tmp_path / "stores" / "ubisoft").mkdir(parents=True)
    (tmp_path / "stores" / "ubisoft" / "installer.py").write_text(
        "async def cleanup():\n    pass\n", encoding="utf-8",
    )
    monkeypatch.setattr(mod, "PY", tmp_path)
    monkeypatch.setattr(
        mod, "parse_vendor_log_stores", lambda: {"battlenet", "ubisoft"},
    )

    assert mod.find_unsalvaged_vendor_logs() == {"ubisoft"}


def test_check10_honours_the_opt_out_marker(
    monkeypatch, tmp_path: Path, mod,
) -> None:
    """``# no-vendor-salvage: <reason>``, in the house style."""
    store = tmp_path / "stores" / "ghoststore"
    store.mkdir(parents=True)
    (store / "install.py").write_text(
        "# no-vendor-salvage: the client writes its logs outside the prefix\n"
        "async def cleanup():\n    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "PY", tmp_path)
    monkeypatch.setattr(mod, "parse_vendor_log_stores", lambda: {"ghoststore"})

    assert mod.find_unsalvaged_vendor_logs() == set()
    assert mod.count_exempt_vendor_salvage() == 1


def test_check10_ignores_a_store_with_globs_but_no_package(
    monkeypatch, tmp_path: Path, mod,
) -> None:
    """A glob row outliving its store is a stale row, not a missing call.

    Failing here would blame the wrong thing and could not be fixed by
    adding a call anywhere.
    """
    monkeypatch.setattr(mod, "PY", tmp_path)
    monkeypatch.setattr(mod, "parse_vendor_log_stores", lambda: {"removed"})

    assert mod.find_unsalvaged_vendor_logs() == set()


def test_check10_refuses_rather_than_passes_when_the_table_is_gone(
    monkeypatch, tmp_path: Path, mod,
) -> None:
    """A renamed table must fail loudly, not report an empty store set.

    An empty set would make the check silently vacuous — the exact failure
    mode it exists to catch, reproduced in the checker itself.
    """
    shared = tmp_path / "stores" / "shared"
    shared.mkdir(parents=True)
    (shared / "prefix_forensics.py").write_text(
        "OTHER_TABLE = {}\n", encoding="utf-8",
    )
    monkeypatch.setattr(mod, "PY", tmp_path)

    with pytest.raises(SystemExit):
        mod.parse_vendor_log_stores()


# ========================================================= #
# 11. A promoted shared helper is defined exactly once
# ========================================================= #
def _plant(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def helper_tree(monkeypatch, tmp_path: Path, mod):
    """A tree holding only the owning module for one tracked helper."""
    _plant(tmp_path, "stores/shared/install_status.py",
           "def merge_install_status(owned, installed):\n    return owned\n")
    monkeypatch.setattr(mod, "PY", tmp_path)
    monkeypatch.setattr(
        mod, "SHARED_HELPERS",
        {"merge_install_status": "stores/shared/install_status.py"},
    )
    return tmp_path


def test_check11_passes_when_the_helper_is_defined_once(helper_tree, mod) -> None:
    assert mod.find_duplicate_shared_helpers() == []


def test_check11_catches_a_copy_pasted_back_into_a_store(
    helper_tree: Path, mod,
) -> None:
    """The §3.4 defect, reintroduced."""
    _plant(helper_tree, "stores/epic/library.py",
           "def merge_install_status(owned, installed):\n    return owned\n")

    strays = mod.find_duplicate_shared_helpers()

    assert [(n, w) for n, _, w in strays] == [
        ("merge_install_status", "stores/epic/library.py:1"),
    ]


def test_check11_catches_a_copy_written_as_a_method(
    helper_tree: Path, mod,
) -> None:
    """Four of the five §3.4 duplicates were methods, not module functions."""
    _plant(helper_tree, "stores/gog/store.py",
           "class GOGStore:\n"
           "    def merge_install_status(self, owned, installed):\n"
           "        return owned\n")

    assert len(mod.find_duplicate_shared_helpers()) == 1


def test_check11_catches_an_async_copy(helper_tree: Path, mod) -> None:
    _plant(helper_tree, "stores/amazon/lib.py",
           "async def merge_install_status(owned, installed):\n    return owned\n")

    assert len(mod.find_duplicate_shared_helpers()) == 1


def test_check11_reports_the_owning_module_in_the_failure(
    helper_tree: Path, mod,
) -> None:
    """The message has to say where the code should have come from."""
    _plant(helper_tree, "stores/epic/library.py",
           "def merge_install_status(owned, installed):\n    return owned\n")

    _, owner, _ = mod.find_duplicate_shared_helpers()[0]

    assert owner == "stores/shared/install_status.py"


def test_check11_honours_the_marker_on_the_def_line(
    helper_tree: Path, mod,
) -> None:
    _plant(helper_tree, "stores/epic/library.py",
           "def merge_install_status(o, i):  "
           "# intentional-divergence: different contract\n    return o\n")

    assert mod.find_duplicate_shared_helpers() == []
    assert mod.count_intentional_divergences() == 1


def test_check11_honours_a_marker_two_comment_lines_up(
    helper_tree: Path, mod,
) -> None:
    """The live divergence in the tree needs two lines to state its reason.

    A first cut looked only one line above the ``def`` and red-flagged
    Ubisoft's legitimate hook.
    """
    _plant(helper_tree, "stores/epic/library.py",
           "# intentional-divergence: the two contracts are inverses and\n"
           "# neither can do the other's job\n"
           "def merge_install_status(o, i):\n    return o\n")

    assert mod.find_duplicate_shared_helpers() == []


def test_check11_marker_does_not_reach_across_code(
    helper_tree: Path, mod,
) -> None:
    """Only the contiguous comment run above the def counts.

    Otherwise one marker anywhere in a file would exempt every later
    definition in it — a group-level justification standing in for a
    per-name one, which is audit item 27's defect.
    """
    _plant(helper_tree, "stores/epic/library.py",
           "# intentional-divergence: applies to the helper below only\n"
           "def unrelated():\n    pass\n"
           "def merge_install_status(o, i):\n    return o\n")

    assert len(mod.find_duplicate_shared_helpers()) == 1


def test_check11_ignores_a_mere_mention_of_the_name(
    helper_tree: Path, mod,
) -> None:
    """Importing or calling the shared helper is the desired state."""
    _plant(helper_tree, "stores/epic/store.py",
           "from unifideck.stores.shared.install_status import (\n"
           "    merge_install_status,\n)\n\n"
           "def get_library(owned, installed):\n"
           "    return merge_install_status(owned, installed)\n")

    assert mod.find_duplicate_shared_helpers() == []


def test_check11_does_not_fire_mid_word(helper_tree: Path, mod) -> None:
    _plant(helper_tree, "stores/epic/library.py",
           "def merge_install_status_legacy(o, i):\n    return o\n")

    assert mod.find_duplicate_shared_helpers() == []


def test_check11_every_tracked_owner_exists(mod) -> None:
    """A row whose owning module has moved would make the check vacuous.

    Same failure mode as check 10's renamed table: the helper name would
    stop matching anything and the row would silently guard nothing.
    """
    for name, owner in mod.SHARED_HELPERS.items():
        path = mod.PY / owner
        assert path.is_file(), f"{name}: owning module {owner} is missing"
        pattern = f"def {name}("
        assert pattern in path.read_text(), f"{name} is not defined in {owner}"


# ========================================================= #
# 12. Every first-party module is imported by something
# ========================================================= #
@pytest.fixture
def import_tree(monkeypatch, tmp_path: Path, mod):
    """A minimal package: one importer, one imported module.

    ``_module_name`` names a file relative to ``PY.parent``, so the package
    directory must literally be called ``unifideck`` for the dotted names to
    come out as ``unifideck.*``.
    """
    pkg = tmp_path / "unifideck"
    _plant(pkg, "__init__.py", "")
    # The package __init__ re-exports the leaf importer, which is how a real
    # package makes its entry modules reachable. Without it ``consumer`` is
    # itself an orphan — correctly, since _IMPORTER_ROOTS is empty here.
    _plant(pkg, "live/__init__.py", "from .consumer import use\n")
    _plant(
        pkg, "live/consumer.py",
        "from unifideck.live.helper import thing\n\n"
        "def use():\n    return thing()\n",
    )
    _plant(pkg, "live/helper.py", "def thing():\n    return 1\n")
    monkeypatch.setattr(mod, "PY", pkg)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_IMPORTER_ROOTS", ())
    return pkg


def _orphan_names(mod) -> list[str]:
    return [name for name, _path in mod.find_unimported_modules()]


def test_check12_passes_when_every_module_is_imported(import_tree, mod) -> None:
    assert _orphan_names(mod) == []


def test_check12_catches_a_module_nothing_imports(import_tree: Path, mod) -> None:
    """The shape that shipped: an empty stub beside a real implementation.

    ``launcher/fixes/`` and ``launcher/language_setup/`` shadowed the real
    ``launcher/proton/*`` with identical module names, every file a
    ``# TODO: implement`` stub, from the initial commit onwards. Importing
    the stub resolved, did nothing, and raised nothing.
    """
    _plant(import_tree, "live/orphan.py", "def never_called():\n    return 0\n")
    assert _orphan_names(mod) == ["unifideck.live.orphan"]


def test_check12_resolves_relative_imports_against_the_package(
    import_tree: Path, mod,
) -> None:
    """The half a naive scan gets wrong, and the reason to test it.

    ``from ..cloud import x`` inside ``live/nested/`` must resolve to
    ``unifideck.live.cloud.x``. A first cut that skipped this reported a
    dozen live modules as orphans, which would have made the gate useless.
    """
    _plant(import_tree, "live/cloud/__init__.py", "")
    _plant(import_tree, "live/cloud/target.py", "VALUE = 1\n")
    _plant(import_tree, "live/nested/__init__.py", "")
    _plant(
        import_tree, "live/nested/importer.py",
        "from ..cloud.target import VALUE\n\nX = VALUE\n",
    )
    _plant(
        import_tree, "live/consumer.py",
        "from unifideck.live.helper import thing\n"
        "from unifideck.live.nested.importer import X\n\n"
        "def use():\n    return thing(), X\n",
    )
    assert _orphan_names(mod) == []


def test_check12_honours_the_entry_point_marker(import_tree: Path, mod) -> None:
    _plant(
        import_tree, "live/cli_main.py",
        "# entry-point: run by bin/unifideck-launcher\ndef main():\n    return 0\n",
    )
    assert _orphan_names(mod) == []


def test_check12_honours_the_unimported_marker(import_tree: Path, mod) -> None:
    """Distinct from ``# entry-point:`` on purpose.

    "Reached by a process" and "dead, tracked by an open item" are different
    claims; conflating them is how an allowlist stops meaning anything.
    """
    _plant(
        import_tree, "live/known_dead.py",
        '"""Doc.\n\n# unimported: audit register item 37 — pending a product call.\n"""\n',
    )
    assert _orphan_names(mod) == []


def test_check12_honours_a_main_guard(import_tree: Path, mod) -> None:
    _plant(
        import_tree, "live/script.py",
        'def main():\n    return 0\n\n\nif __name__ == "__main__":\n    main()\n',
    )
    assert _orphan_names(mod) == []


def test_check12_ignores_package_inits(import_tree: Path, mod) -> None:
    """An ``__init__.py`` is reached by importing its package, not by name."""
    _plant(import_tree, "live/lonely/__init__.py", "")
    assert _orphan_names(mod) == []


def test_check12_counts_a_from_package_import_of_a_submodule(
    import_tree: Path, mod,
) -> None:
    """``from pkg import submodule`` imports a module, not a symbol."""
    _plant(import_tree, "live/sub/__init__.py", "")
    _plant(import_tree, "live/sub/leaf.py", "Y = 2\n")
    _plant(
        import_tree, "live/consumer.py",
        "from unifideck.live.helper import thing\n"
        "from unifideck.live.sub import leaf\n\n"
        "def use():\n    return thing(), leaf.Y\n",
    )
    assert _orphan_names(mod) == []


def test_check12_does_not_treat_a_test_importer_as_production_use(
    import_tree: Path, mod, tmp_path: Path,
) -> None:
    """A module imported only by a test is still dead production code.

    ``tests/`` is deliberately absent from ``_IMPORTER_ROOTS``. The SGDB
    ``match`` shim was exactly this: no production importer, one test
    pinning it, and a docstring claiming the package imported it.
    """
    _plant(import_tree, "live/tested_only.py", "def f():\n    return 1\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_it.py").write_text(
        "from unifideck.live.tested_only import f\n", encoding="utf-8",
    )
    assert _orphan_names(mod) == ["unifideck.live.tested_only"]


def test_check12_is_clean_on_the_real_tree(mod) -> None:
    """The gate must pass against the live repo, or it gets switched off."""
    assert mod.find_unimported_modules() == []


# ========================================================= #
# 13. No function body duplicated across modules
# ========================================================= #
@pytest.fixture
def shape_tree(monkeypatch, tmp_path: Path, mod):
    """A package with one distinctive multi-statement helper."""
    pkg = tmp_path / "unifideck"
    body = (
        "def canonical(value):\n"
        "    forms = [str(value)]\n"
        "    if value > 10:\n"
        "        forms.append(str(value - 10))\n"
        "    return forms\n"
    )
    _plant(pkg, "shared/helper.py", body)
    monkeypatch.setattr(mod, "PY", pkg)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    return pkg


def _groups(mod) -> list[list[str]]:
    return sorted(members for _sig, members in mod.find_duplicate_bodies())


def test_check13_passes_when_each_body_is_unique(shape_tree, mod) -> None:
    assert _groups(mod) == []


def test_check13_catches_a_copy_that_was_renamed(shape_tree: Path, mod) -> None:
    """The escape item 47 is about, and check 11 cannot see.

    Check 11 matches a promoted helper by **name**, so
    ``_appid_key_candidates`` sat beside ``appid_candidates`` — and inlined a
    third time — for the life of the project (register item 20). Matching on
    body shape means a rename cannot hide a copy.
    """
    _plant(
        shape_tree, "stores/copy.py",
        "def _renamed_copy(v):\n"
        "    forms = [str(v)]\n"
        "    if v > 10:\n"
        "        forms.append(str(v - 10))\n"
        "    return forms\n",
    )
    groups = _groups(mod)
    assert len(groups) == 1
    assert groups[0] == [
        "shared/helper.py::canonical",
        "stores/copy.py::_renamed_copy",
    ]


def test_check13_ignores_a_body_below_the_size_floor(
    shape_tree: Path, mod,
) -> None:
    """Two-line getters collide for uninteresting reasons.

    Comparing them turns the gate into noise, which is how a gate gets
    switched off rather than fixed (audit §2.1's lookbehind lesson).
    """
    _plant(shape_tree, "a/one.py", "def f(x):\n    return x.value\n")
    _plant(shape_tree, "b/two.py", "def g(y):\n    return y.value\n")
    assert _groups(mod) == []


def test_check13_keeps_attribute_names_significant(
    shape_tree: Path, mod,
) -> None:
    """Erasing attribute names too would match every try/except wrapper.

    These two have identical structure but call different methods, so they
    are different functions and must not be reported.
    """
    _plant(
        shape_tree, "a/one.py",
        "def f(p):\n    p.mkdir()\n    p.write_text('x')\n    return True\n",
    )
    _plant(
        shape_tree, "b/two.py",
        "def g(p):\n    p.unlink()\n    p.touch()\n    return True\n",
    )
    assert _groups(mod) == []


def test_check13_ignores_duplicates_inside_one_module(
    shape_tree: Path, mod,
) -> None:
    """Two variants in one file are a local style choice, not drift."""
    # A shape distinct from the fixture's helper, so the only possible
    # match is the same-file pair — which must be ignored.
    _plant(
        shape_tree, "a/one.py",
        "def f(v):\n"
        "    out = v.encode()\n"
        "    out = out.strip()\n"
        "    return out.decode()\n"
        "\n\n"
        "def f2(w):\n"
        "    out = w.encode()\n"
        "    out = out.strip()\n"
        "    return out.decode()\n",
    )
    assert _groups(mod) == []


def test_check13_baseline_is_still_accurate(mod) -> None:
    """A stale baseline row silently widens the gate.

    Every grandfathered group must still exist; one that no longer does has
    been fixed and its row should go, shrinking the list.
    """
    baseline = mod._load_shape_baseline()
    if not baseline:
        pytest.skip("no baseline in this checkout")
    live = [frozenset(members) for _sig, members in mod.find_duplicate_bodies()]
    # A baselined group is still "real" if any live group is a subset of it:
    # consolidating some of its members shrinks the group rather than
    # removing it. Only a row with no live subset at all is stale.
    stale = sorted(
        sorted(row) for row in baseline
        if not any(found <= row for found in live)
    )
    assert stale == [], (
        f"these grandfathered duplicate groups no longer exist — remove them "
        f"from {mod.SHAPE_BASELINE.name}: {stale}"
    )


def test_check13_is_clean_on_the_real_tree(mod) -> None:
    assert mod.report_duplicate_bodies() == 0


def test_check13_a_growing_group_is_not_grandfathered(
    shape_tree: Path, mod, monkeypatch,
) -> None:
    """Shrink-only means shrinking is fine and growing is not.

    A subset of a baselined group is partial consolidation — reporting it
    would red the gate for making progress, and the honest response to that
    is to put the copy back. A *superset* is a new copy and must fail.
    """
    monkeypatch.setattr(
        mod, "_load_shape_baseline",
        lambda: [frozenset({"a/one.py::f", "b/two.py::g"})],
    )
    body = (
        "def {name}(v):\n"
        "    out = v.encode()\n"
        "    out = out.strip()\n"
        "    return out.decode()\n"
    )
    _plant(shape_tree, "a/one.py", body.format(name="f"))
    _plant(shape_tree, "b/two.py", body.format(name="g"))
    assert mod.report_duplicate_bodies() == 0, "the exact group is grandfathered"

    _plant(shape_tree, "c/three.py", body.format(name="h"))
    assert mod.report_duplicate_bodies() == 1, "a third copy must fail"
