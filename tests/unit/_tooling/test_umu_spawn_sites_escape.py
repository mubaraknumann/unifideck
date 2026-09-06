"""Every direct umu spawn must escape Steam's pressure-vessel container.

Field bug (2026-08-12 bundle, twice over): the escape was added to the one
spawn point in ``umu_runtime`` and assumed to cover everything, but four other
call sites built their own ``[python_bin, umu_wrapper, ...]`` argv and handed
it straight to ``create_subprocess_exec``. Under Steam Force-Compat those ran
*nested* inside pressure-vessel and failed:

* ``prefix_init`` — six ``createprefix`` + one ``wineboot --init``, every one
  exit 1 with ``bwrap: execvp true: No such file or directory``, so the prefix
  was never built;
* ``gog_setup`` — nine straight rc=1 (scriptinterpreter + all four
  vcredists), silently leaving the prefix without its redistributables.

The bug is easy to reintroduce: adding a new umu invocation is a two-line
change, and forgetting the escape breaks only Force-Compat, which no
Deck-based test exercises. So assert the property structurally instead.

``escape_argv`` is a no-op outside a container, so the rule costs nothing on
the normal path.
"""
from __future__ import annotations

from pathlib import Path

_LAUNCHER = (
    Path(__file__).resolve().parents[3]
    / "py_modules" / "unifideck" / "launcher"
)
# Building a umu argv is signalled by ``umu_wrapper``; spawning it yourself by
# ``create_subprocess_exec``. Files that only build an argv and hand it to
# ``run_umu_with_retry`` (the already-escaped path) match the first but not
# the second, so they are correctly ignored — as is ``compat/gog.py``, whose
# only direct spawn is Comet, a native Linux binary that must NOT be escaped.
_BUILDS_UMU_ARGV = "umu_wrapper"
_SPAWNS_DIRECTLY = "create_subprocess_exec"
_ESCAPES = "escape_argv"


def _sources() -> list[Path]:
    return sorted(
        p for p in _LAUNCHER.rglob("*.py") if "__pycache__" not in p.parts
    )


def test_direct_umu_spawns_escape_the_container():
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        if _BUILDS_UMU_ARGV not in text or _SPAWNS_DIRECTLY not in text:
            continue
        if _ESCAPES not in text:
            offenders.append(path.relative_to(_LAUNCHER.parents[2]))
    assert not offenders, (
        "these files build a umu argv and spawn it directly without "
        "escape_argv, so they will run nested inside Steam's "
        "pressure-vessel when Force-Compat is set on the shortcut: "
        + ", ".join(str(p) for p in offenders)
    )


def test_guard_actually_matches_the_known_spawn_sites():
    """Guard the guard: if the signals stop matching, the test above passes
    vacuously and the regression walks back in unnoticed."""
    matched = {
        path.name
        for path in _sources()
        if _BUILDS_UMU_ARGV in (text := path.read_text(encoding="utf-8"))
        and _SPAWNS_DIRECTLY in text
    }
    # The four sites fixed on 2026-08-13, with ``gog_setup/common.py``
    # replaced by ``setup_run.py``: the GOG helper's spawn was hoisted there
    # as the single owner when the Ubisoft registry writers were moved off
    # raw Wine and onto umu, so ``common.py`` now only delegates.
    # ``umu_runtime`` is deliberately absent: it is handed an already-built
    # argv rather than naming ``umu_wrapper`` itself, and it has escaped
    # since the original fix.
    expected = {
        "prefix_init.py",
        "setup_run.py",         # infrastructure/setup_run.py
        "epic_prerequisites.py",
        "battlenet.py",
    }
    missing = expected - matched
    assert not missing, (
        "the detection signals no longer match known umu spawn sites "
        f"({sorted(missing)}) — update this guard rather than deleting it"
    )
