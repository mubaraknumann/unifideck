"""Guard test — scripts/validate_event_wiring.py.

The wiring validator is a CI gate, and a gate nobody has watched fail is
indistinguishable from one that passes everything. Audit §1.1.4's lesson,
applied to the guard itself: verify it against a planted violation, not just
against a clean tree.

What is pinned here:

1. the real repo source is fully wired (exit 0) and the exemption count is
   printed, so the opt-out set cannot grow quietly;
2. every ``# unwired:`` marker names a real Events member — a marker left
   behind on a deleted event silently exempts nothing and hides the next one;
3. each failure class flips the exit code and names the RIGHT missing half —
   an emitter with no consumer, a consumer with no emitter, and each of the
   three frontend legs separately. "Something is wrong with the frontend" is
   not a usable diagnosis when the fix differs per leg;
4. the retired events from audit §1.3 stay retired, since re-declaring one is
   how the original defect shipped.

The clean-source case runs the script as a subprocess against the real repo;
the failure cases drive the module's own functions against synthetic inputs so
no test ever mutates the working tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _find_script() -> Path | None:
    from tests.unit._repo_root import find_repo_file

    return find_repo_file("scripts/validate_event_wiring.py")


@pytest.fixture(scope="module")
def script_path() -> Path:
    p = _find_script()
    if p is None:
        pytest.skip(
            "scripts/validate_event_wiring.py not found "
            "(set UNIFIDECK_REPO_ROOT to the checkout root)")
    return p


@pytest.fixture(scope="module")
def mod(script_path: Path):
    """Import the script so its tables and helpers can be inspected."""
    spec = importlib.util.spec_from_file_location(
        "_vew_under_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=180,
    )


# ========================================================= #
# 1. Clean run against the real source
# ========================================================= #
def test_passes_against_real_source(script_path: Path) -> None:
    """Every event is wired or explicitly exempt."""
    res = _run(script_path)
    assert res.returncode == 0, (
        f"wiring gate failed against real source:\n{res.stdout}\n{res.stderr}")
    assert "event wiring valid" in res.stdout


def test_the_exemption_count_is_printed(script_path: Path) -> None:
    """An allowlist that can grow quietly is not a gate."""
    res = _run(script_path)
    assert "deliberate exemption(s)" in res.stdout


# ========================================================= #
# 2. The opt-out markers are honest
# ========================================================= #
def test_every_marker_names_a_real_event(mod) -> None:
    from unifideck.core.types.events import Events

    names = {e.name for e in Events}
    for marked in mod.exemptions():
        assert marked in names, (
            f"'# unwired:' marker on {marked!r}, which is not an Events "
            "member — it exempts nothing and hides the next real finding")


def test_every_marker_carries_a_reason(mod) -> None:
    """``# unwired:`` with nothing after it would be a silent allowlist row."""
    for name, reason in mod.exemptions().items():
        assert reason.strip(), f"{name} has an empty '# unwired:' reason"


def test_indirect_tables_only_name_real_events(mod) -> None:
    """These are the two places the static check is deliberately blind.

    Every entry suppresses a finding, so a stale one is a hole. Keeping them
    pinned to the enum means a retired event cannot leave a hole behind.
    """
    from unifideck.core.types.events import Events

    names = {e.name for e in Events}
    for table_name in ("INDIRECT_EMITTERS", "INDIRECT_FRONTEND_SUBSCRIBERS"):
        for event in getattr(mod, table_name):
            assert event in names, (
                f"{table_name} names {event!r}, which is not an Events member")


# ========================================================= #
# 3. Each failure class is detected, with the right diagnosis
# ========================================================= #
def test_an_emit_on_the_next_line_is_still_seen(mod) -> None:
    """The dominant style in this tree. A line-oriented gate would miss it.

    If this regressed, the gate would report dozens of false positives, which
    trains people to ignore it — worse than having no gate.
    """
    import ast

    tree = ast.parse(
        "async def f(bus):\n"
        "    await bus.emit(\n"
        "        Events.SYNC_SKIPPED,\n"
        "        store='microsoft',\n"
        "    )\n",
    )
    assert "SYNC_SKIPPED" in mod._emitted_names(tree)


def test_a_decorator_subscription_is_seen(mod) -> None:
    import ast

    tree = ast.parse(
        "class S:\n"
        "    @subscribe(Events.GAME_STOPPED)\n"
        "    async def h(self, **kw): ...\n",
    )
    assert "GAME_STOPPED" in mod._subscribed_names(tree)


def test_a_string_form_subscription_is_seen(mod) -> None:
    """The frontend-facing half of the bus accepts raw names too."""
    import ast

    tree = ast.parse("bus.subscribe('sync_complete', h)\n")
    assert "SYNC_COMPLETE" in mod._subscribed_names(tree)


def test_watched_events_cannot_be_vouched_for_by_a_sibling_list(mod) -> None:
    """``event-bus-client.ts`` holds three arrays of event names.

    A whole-file search would let membership in ``IMPERATIVE_EVENTS`` or
    ``STALE_ON_RELOAD_EVENTS`` stand in for being polled — the same
    self-vouching hole ``validate_architecture.py`` had to close for
    ``rpc-routes.ts``. Only the WATCHED_EVENTS block may answer this.
    """
    block = mod._watched_block()
    assert block, "WATCHED_EVENTS block not located"
    assert "IMPERATIVE_EVENTS" not in block
    assert "STALE_ON_RELOAD_EVENTS" not in block
    # A sanity anchor: something known-polled is inside the isolated block.
    assert '"sync_progress"' in block


# ========================================================= #
# 4. The retired events stay retired
# ========================================================= #
@pytest.mark.parametrize(
    "name",
    [
        "SUSPEND", "RESUME",              # playtime sleep accounting
        "STORE_ERROR",                    # never had an emitter, ever
        "SUBSCRIPTION_DETECTED",
        "SUBSCRIPTION_EXPIRED",
        "SUBSCRIPTION_CHECK_FAILED",      # superseded by SYNC_SKIPPED
        "PLUGIN_LOADED", "PLUGIN_UNLOADING",   # dead in both directions
        "TOAST_NOTIFICATION",             # retired earlier, audit §1.1.2
        "GAME_INSTALLED",                 # retired earlier, audit §1.1.1
    ],
)
def test_retired_events_are_not_reintroduced(name: str) -> None:
    """Re-declaring one is exactly how each of these shipped broken.

    A member with no emitter still satisfies every call site that references
    it, so the enum is the only place the absence is visible.
    """
    from unifideck.core.types.events import Events

    assert name not in {e.name for e in Events}
