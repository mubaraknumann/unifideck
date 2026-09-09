"""Guard test — scripts/validate_event_schemas.py.

The event-schema validator is a CI gate: if it silently breaks
(stale schema, broken extraction, wrong exit code) a whole
class of event-contract regressions stops being caught. This
module pins its behaviour:

1. against the *real* repo source it must report success
   (exit 0) — i.e. CANONICAL_SCHEMA is in sync with the code;
2. every CANONICAL_SCHEMA key is a real Events enum member
   (the failure mode that made the original script useless:
   it declared AUTH_STARTED / ARTWORK_READY which no longer
   existed, so the comparison never matched them);
3. the three failure classes each flip the exit code to 1
   (unexpected kwarg, phantom schema key, emitted-but-
   undeclared), so the gate cannot pass while broken;
4. extraction noise is filtered — the priority dispatcher's
   ``bus.emit(item.event, ...)`` must NOT surface as a
   phantom event named ``"event"``;
5. single-emitter events are only emitted from their owning
   subsystem, and that check reports a violation rather than
   passing regardless (audit item #4).

Resolution of the repo root is robust (env var → walk up from
the unifideck package → known locations) because the suite
runs out-of-tree. If the script or repo can't be located the
test SKIPS rather than fails — a missing checkout is an
environment issue, not a regression. (Note: the strict CI
forbids skips, so in CI this never silently skips; locally it
degrades gracefully.)
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


# Files/dirs that only ever exist at a repo checkout root.
# Used to recognise the root by structure instead of by a
# hard-coded absolute path, so this works in CI, in a local
# clone, in a worktree, or wherever the suite is unpacked.
def _find_script() -> Path | None:
    """Locate scripts/validate_event_schemas.py via the
    shared, structurally-resolved repo root (no hard-coded
    paths)."""
    from tests.unit._repo_root import find_repo_file

    return find_repo_file("scripts/validate_event_schemas.py")


@pytest.fixture(scope="module")
def script_path() -> Path:
    p = _find_script()
    if p is None:
        pytest.skip(
            "scripts/validate_event_schemas.py not found "
            "(set UNIFIDECK_REPO_ROOT to the checkout root)")
    return p


@pytest.fixture(scope="module")
def script_module(script_path: Path):
    """Import the script as a module so its functions and
    CANONICAL_SCHEMA can be inspected directly."""
    spec = importlib.util.spec_from_file_location(
        "_ves_under_test", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=120,
    )


# ========================================================= #
# 1. Clean run against the real source
# ========================================================= #
def test_passes_against_real_source(
    script_path: Path,
) -> None:
    """The committed CANONICAL_SCHEMA matches the codebase:
    exit 0 and the success marker on stdout."""
    res = _run(script_path)
    assert res.returncode == 0, (
        f"validator failed against real source:\n"
        f"{res.stdout}\n{res.stderr}")
    assert "event schemas valid" in res.stdout


# ========================================================= #
# 2. Every schema key is a real enum member
# ========================================================= #
def test_schema_keys_are_real_events(
    script_module,
) -> None:
    """No phantom keys (the AUTH_STARTED / ARTWORK_READY
    failure mode that silently neutered the old script)."""
    phantom = (
        set(script_module.CANONICAL_SCHEMA)
        - script_module.VALID_EVENTS
    )
    assert not phantom, (
        f"CANONICAL_SCHEMA has non-enum keys: "
        f"{sorted(phantom)}")
    assert script_module.validate_schema_keys() == 0


def _load_with_patched_schema(
    script_path: Path, old: str, new: str,
):
    """Import the script as a fresh module with one source
    substitution applied, WITHOUT changing its ROOT (so it
    still resolves the real py_modules/). Returns the module.
    """
    src = script_path.read_text()
    patched = src.replace(old, new)
    assert patched != src, "anchor not found — script changed?"
    import types

    mod = types.ModuleType("_ves_patched")
    mod.__file__ = str(script_path)  # keep ROOT resolution
    code = compile(patched, str(script_path), "exec")
    exec(code, mod.__dict__)  # noqa: S102 - trusted local file
    return mod


# ========================================================= #
# 3. The three failure classes each fail the gate
# ========================================================= #
def test_unexpected_kwarg_fails(
    script_path: Path,
) -> None:
    """Narrowing a declared contract below what is actually
    emitted must fail (main() returns 1)."""
    mod = _load_with_patched_schema(
        script_path,
        '"GAME_UNINSTALLED":             {"game_id", "store"},',
        '"GAME_UNINSTALLED":             {"game_id"},')
    assert mod.main() == 1


def test_phantom_schema_key_fails(
    script_path: Path,
) -> None:
    """A CANONICAL_SCHEMA key absent from the Events enum
    must fail fast (main() returns 1) via
    validate_schema_keys()."""
    mod = _load_with_patched_schema(
        script_path,
        '"ACCOUNT_SWITCHED":             {"active_user_id", "new_user"},',
        '"ACCOUNT_SWITCHED":             {"active_user_id", "new_user"},\n'
        '    "DEFINITELY_NOT_AN_EVENT":      {"x"},')
    assert "DEFINITELY_NOT_AN_EVENT" in mod.CANONICAL_SCHEMA
    assert mod.validate_schema_keys() == 1
    assert mod.main() == 1


def test_emitted_but_undeclared_fails(
    script_path: Path,
) -> None:
    """Removing a really-emitted event from the schema must
    surface it as emitted-but-undeclared (main() returns 1)."""
    mod = _load_with_patched_schema(
        script_path,
        '    "STORE_LOGOUT":                 {"store"},\n', "")
    assert "STORE_LOGOUT" not in mod.CANONICAL_SCHEMA
    assert mod.main() == 1


# ========================================================= #
# 4. Extraction noise is filtered
# ========================================================= #
def test_extraction_noise_is_filtered(
    script_module,
) -> None:
    """The priority dispatcher's ``bus.emit(item.event, ...)``
    resolves to the bogus name ``"event"``; walk_sources must
    drop it (and any other non-enum name) rather than report a
    phantom event."""
    target = (
        script_module.ROOT / "py_modules" / "unifideck"
    )
    actual, emitters = script_module.walk_sources(target)
    assert "event" not in actual
    # everything surviving the filter is a real enum member
    assert set(actual) <= script_module.VALID_EVENTS
    # The emitter map is filtered by the same rule, and covers every
    # event the kwargs map does — check_emitter_owners reads it.
    assert set(emitters) == set(actual)


# ========================================================= #
# 5. Single-emitter ownership
# ========================================================= #
def test_download_events_are_owned_by_the_download_service(
    script_module,
) -> None:
    """The live tree must have exactly one DOWNLOAD_* emitter.

    Audit item #4: Epic and Amazon emitted the whole download
    lifecycle a second time from their installers. The kwargs
    check catches a duplicate that invents its own payload; this
    one catches a duplicate that copies the right payload from the
    wrong place.
    """
    target = script_module.ROOT / "py_modules" / "unifideck"
    _actual, emitters = script_module.walk_sources(target)

    assert script_module.check_emitter_owners(emitters) == 0
    for event, owner in script_module.EMITTER_OWNERS.items():
        for path in emitters.get(event, set()):
            assert path.startswith(owner), f"{event} emitted from {path}"


def test_an_emitter_outside_its_owning_subsystem_is_reported(
    script_module,
) -> None:
    """The check must actually bite, not just return 0 forever."""
    owned = next(iter(script_module.EMITTER_OWNERS))
    violations = script_module.check_emitter_owners(
        {owned: {"stores/epic/install.py", "services/download/worker.py"}},
    )
    assert violations == 1  # the store file only


# ========================================================= #
# Subscribe-side arm (audit correction C-2)                 #
# ========================================================= #
def test_the_live_tree_has_no_subscriber_reading_an_undeclared_key(
    script_module,
) -> None:
    """The regression this arm exists for.

    ``validate_event_schemas`` was emit-side only, so a handler reading a
    key no emitter sends was invisible. Three events shipped that defect:
    ``GAME_INSTALLED`` (``app_id`` vs ``game_id``), ``TOAST_NOTIFICATION``
    (``params`` vs ``i18n_params``), and ``GAME_STOPPED`` (``rc`` vs
    ``exit_code``) — the last was still live when this check was written,
    and it meant the circuit breaker could never reset on a good launch.
    """
    target = script_module.ROOT / "py_modules" / "unifideck"
    subscribers = script_module.walk_subscribers(target)

    assert subscribers, "no @subscribe handlers found — the walker is broken"
    assert script_module.check_subscriber_reads(subscribers) == 0


def test_a_subscriber_reading_a_phantom_key_is_reported(script_module) -> None:
    """The check must bite. Drives the real GAME_STOPPED contract."""
    declared = script_module.CANONICAL_SCHEMA["GAME_STOPPED"]
    assert "exit_code" in declared and "rc" not in declared

    errors = script_module.check_subscriber_reads(
        [("GAME_STOPPED", "services/x.py", "_on_game_stopped", {"store", "rc"})],
    )
    assert errors == 1


def test_a_subscriber_reading_only_declared_keys_passes(script_module) -> None:
    errors = script_module.check_subscriber_reads(
        [(
            "GAME_STOPPED",
            "services/x.py",
            "_on_game_stopped",
            {"store", "game_id", "exit_code", "elapsed_seconds"},
        )],
    )
    assert errors == 0


def test_an_event_with_no_declared_schema_is_skipped(script_module) -> None:
    """Nothing to compare against; :func:`compare` already reports it."""
    assert "NOT_A_REAL_EVENT" not in script_module.CANONICAL_SCHEMA
    errors = script_module.check_subscriber_reads(
        [("NOT_A_REAL_EVENT", "services/x.py", "_h", {"anything"})],
    )
    assert errors == 0


def test_every_tolerated_read_still_corresponds_to_a_real_handler(
    script_module,
) -> None:
    """A stale exemption silently widens the gate.

    Same failure mode as a ``# unwired:`` marker left on a deleted event:
    the row exempts nothing and hides the next real mismatch. Keyed
    ``<module>::<handler>`` precisely so a rename shows up here.
    """
    target = script_module.ROOT / "py_modules" / "unifideck"
    if not script_module.TOLERATED_SUBSCRIBER_READS:
        return  # empty is the goal; nothing to go stale
    live = {
        f"{rel}::{handler}"
        for _event, rel, handler, _keys in script_module.walk_subscribers(target)
    }
    for key in script_module.TOLERATED_SUBSCRIBER_READS:
        assert key in live, (
            f"TOLERATED_SUBSCRIBER_READS names {key!r}, which is not a live "
            f"@subscribe handler — delete the row or fix the path"
        )


def test_a_tolerated_read_does_not_mask_a_second_undeclared_key(
    script_module,
) -> None:
    """Tolerating one fallback must not wave through a real defect beside it."""
    # Drives the mechanism with a synthetic entry so the test keeps working
    # once the real table is empty — which it now is, because both founding
    # entries were deleted rather than carried (audit register item 41).
    monkeypatched = {"services/x.py::_h": {"tolerated_key"}}
    script_module.TOLERATED_SUBSCRIBER_READS.update(monkeypatched)
    key = "services/x.py::_h"
    rel, handler = key.split("::", 1)
    tolerated = script_module.TOLERATED_SUBSCRIBER_READS[key]

    errors = script_module.check_subscriber_reads(
        [(
            "POST_SYNC_PHASE_CHANGED",
            rel,
            handler,
            {*tolerated, "a_genuinely_wrong_key"},
        )],
    )
    assert errors == 1


# ========================================================= #
# The two LAUNCHER_STAGE payload builders must agree        #
# ========================================================= #
def test_the_two_toast_builders_produce_the_same_payload_shape() -> None:
    """``emit_stage`` and ``launcher_toast`` are one contract in two places.

    Both build a ``LAUNCHER_STAGE`` payload and both end up in the same
    bridge file, but they exist separately for a real reason: the deep launch
    helpers (umu retry, winetricks, prefix init) are plain functions several
    frames below anything holding an ``EventBus``, so ``launcher_toast``
    writes to the bridge directly. 38 of the 46 backend toast call sites use
    it.

    ``launcher_toast``'s own docstring states the invariant and admits it is
    hand-maintained: *"The two builders must stay in step: a field one
    produces and the other doesn't is a payload that renders differently
    depending on which process emitted it."* Nothing checked it — the same
    hand-maintained-pair shape as ``uses_wine`` ↔ ``WRAPPER_STORES`` in audit
    §3.1, where the pair was fine right up until it wasn't.

    They agree today (7 keys each). This pins that.
    """
    import ast
    from pathlib import Path

    from tests.unit._repo_root import find_repo_file

    def payload_keys(rel: str, fn_name: str) -> set[str]:
        path = find_repo_file(rel)
        assert path is not None, rel
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == fn_name
            ):
                keys: set[str] = set()
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Subscript)
                        and isinstance(sub.slice, ast.Constant)
                        and getattr(sub.value, "id", "") == "payload"
                    ):
                        keys.add(sub.slice.value)
                    if isinstance(sub, ast.Dict):
                        for k in sub.keys:
                            if isinstance(k, ast.Constant) and isinstance(
                                k.value, str,
                            ):
                                keys.add(k.value)
                return keys
        raise AssertionError(f"{fn_name} not found in {rel}")

    bus_side = payload_keys(
        "py_modules/unifideck/launcher/rpc.py", "emit_stage",
    )
    bridge_side = payload_keys(
        "py_modules/unifideck/launcher/frontend_bridge.py", "launcher_toast",
    )

    assert bus_side, "emit_stage built no payload keys — parser broken?"
    assert bus_side == bridge_side, (
        f"the two LAUNCHER_STAGE builders have drifted.\n"
        f"  only emit_stage:     {sorted(bus_side - bridge_side)}\n"
        f"  only launcher_toast: {sorted(bridge_side - bus_side)}\n"
        f"A field one sends and the other does not renders differently "
        f"depending on which process emitted the toast."
    )
