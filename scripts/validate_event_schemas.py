#!/usr/bin/env python3
"""scripts/validate_event_schemas.py — CI guard for event kwargs.

Static-analysis gate that keeps every ``bus.emit(Events.X, ...)``
call site honest. It walks the whole backend, collects the union
of kwargs passed to each event, and compares that against the
declared contract in ``CANONICAL_SCHEMA``. Any mismatch
(unexpected kwarg, or an event emitted but never declared) fails
the CI.

Usage:
    python3 scripts/validate_event_schemas.py
    echo $?   # 0 = clean, 1 = mismatches, 2 = error

Add to .github/workflows/ before the pytest step. When adding a
new event:
  1. add it to the ``Events`` enum (core/types/events.py),
  2. add its kwargs contract to ``CANONICAL_SCHEMA`` below,
  3. run this script.

──────────────────────────────────────────────────────────────
Why the schema is validated against the enum
──────────────────────────────────────────────────────────────
``SchemaExtractor`` extracts the *event name* from the first
positional argument of every ``.emit()`` / ``.enqueue()`` call.
It accepts two shapes (``Events.SOMETHING`` and a string literal)
and explicitly "can't follow data flow" for anything else.

That last point bites in two ways, both handled here:

  * ``bus.emit(item.event, **item.kwargs)`` in the priority
    dispatcher is an ``ast.Attribute`` whose ``.attr`` is the
    literal string ``"event"`` — the extractor cannot tell it
    apart from ``Events.GAME_STOPPED``. Without filtering it
    surfaces a phantom event named ``event``.
  * ``SECURITY_*`` events are emitted through wrappers
    (``_emit_security_event``, ``audit_emitter``) rather than a
    direct ``Events.X`` literal, so the extractor never sees
    them. They are intentionally absent from CANONICAL_SCHEMA:
    the ``compare`` loop skips events with no observed emit
    (``seen is None``), so declaring them would be dead weight,
    and declaring them with guessed kwargs would be worse.

The robust fix for the first problem is to treat the ``Events``
enum as the source of truth: any extracted name that is not a
real enum member is extraction noise (a variable, an object
attribute, a generic re-emit) and is dropped before comparison.
A start-up check also asserts every CANONICAL_SCHEMA key is a
real enum member, so a typo / renamed / deleted event fails
loudly instead of silently never matching.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py_modules"))

from unifideck.core.types.events import Events  # noqa: E402
from unifideck.event_bus.event_bus_devex import (  # noqa: E402
    SchemaExtractor,
)

# Every name that is a genuine member of the Events enum. Used
# both to drop extraction noise and to validate the schema keys.
VALID_EVENTS: set[str] = {e.name for e in Events}

# The declared kwargs contract per event.
#
# Generated from the actual emit sites (the union of kwargs seen
# across the codebase for each event) and then frozen here as
# the contract. Only events emitted via a literal
# ``Events.X`` / ``"name"`` first argument can be statically
# checked, so events emitted exclusively through wrappers (the
# SECURITY_* family via audit_emitter / _emit_security_event)
# are deliberately not listed — the extractor never observes
# them and ``compare`` skips unobserved events anyway.
#
# When an emit site legitimately gains a kwarg, widen the set
# here in the same change so the contract stays the source of
# truth rather than drifting behind the code.
CANONICAL_SCHEMA: dict[str, set[str]] = {
    "ACCOUNT_SWITCHED":             {"active_user_id", "new_user"},
    "CIRCUIT_STATE_CHANGED":        {"failure_count", "game_id", "is_open", "store", "trigger"},
    "CONFIG_VALIDATION_COMPLETED":  {"defaults_validated", "user_overrides_present"},
    "CONFIG_VALIDATION_FAILED":     {"defaults_validated", "error_count", "first_error_path", "first_error_source", "user_overrides_present"},
    # Every DOWNLOAD_* event carries the queue item and nothing else
    # (COMPLETE adds the Game record the shortcut service needs). Epic and
    # Amazon used to emit a second, store-shaped copy of each — flat
    # ``store``/``game_id``/``install_path`` with no item — which reached
    # the frontend as a duplicate failure toast and a no-op refresh (audit
    # item #4). These sets are deliberately narrow: re-adding a flat kwarg
    # fails this gate. See also EMITTER_OWNERS below.
    "DOWNLOAD_CANCELLED":           {"item"},
    "DOWNLOAD_COMPLETE":            {"game", "item"},
    "DOWNLOAD_FAILED":              {"error", "error_type", "item"},
    "DOWNLOAD_PROGRESS":            {"item"},
    "DOWNLOAD_QUEUED":              {"item"},
    "DOWNLOAD_STARTED":             {"item"},
    "GAME_LAUNCHED":                {"app_id", "game_id", "store", "title"},
    "GAME_STOPPED":                 {"app_id", "elapsed_seconds", "exit_code", "game_id", "store", "terminated_by_signal"},
    "GAME_UNINSTALLED":             {"game_id", "store"},
    # ``game_ids`` is the store's COMPLETE current set of updatable games,
    # not a delta — a game that drops out of it has had its update applied.
    "GAME_UPDATE_AVAILABLE":        {"game_ids", "store"},
    "LAUNCHER_STAGE":               {"action", "duration_ms", "game_id", "game_title", "i18n_key", "i18n_params", "i18n_title_key", "local_snapshot", "priority", "remote_snapshot", "severity", "store"},
    "LIBRARY_SYNC_CANCELLED":       {"cancelled_at_store", "store_count"},
    "LIBRARY_SYNC_COMPLETED":       {"duration_ms", "errors", "game_count", "store_count"},
    "LIBRARY_SYNC_STARTED":         {"started_at_ms", "stores"},
    "METADATA_BACKFILL_COMPLETE":   {"count"},
    "PLAYTIME_UPDATED":             {"duration_secs", "game_id", "store"},
    "PLAYTIME_SYNC_COMPLETE":       {"pushed", "store"},
    "PLAYTIME_SYNC_FAILED":         {"error", "store"},
    "POST_SYNC_PHASE_CHANGED":      {"active", "done", "phase", "run_id", "sync_kwargs", "total"},
    "RUNTIME_PROBES_REPORTED":      {"probes"},
    "SHORTCUT_CREATED":             {"app_id", "is_auth", "store", "title"},
    "SHORTCUT_INSTALL_STATE_CHANGED": {"app_id", "exe_path", "install_path", "installed", "store", "store_game_id"},
    "SHORTCUT_RECONCILE_COMPLETE":  {"added", "kept", "reclaimed", "removed", "run_id", "total"},
    "SHORTCUT_REMOVED":             {"app_id"},
    "STORE_AUTH_COMPLETE":          {"store"},
    "STORE_AUTH_FAILED":            {"error", "store"},
    "STORE_AUTH_STARTED":           {"store"},
    "STORE_LOGOUT":                 {"store"},
    "STORE_REGISTERED":             set(),
    "SYNC_CANCELLED":               set(),
    "SYNC_COMPLETE":                {"duration_ms", "errors", "fetch_artwork", "games", "is_force", "resync_artwork", "run_id", "skip_chain", "stores_synced"},
    "SYNC_FAILED":                  {"error", "store"},
    "SYNC_PROGRESS":                {"current_game", "progress_percent", "status", "store", "synced_games", "total_games"},
    "SYNC_SKIPPED":                 {"reason", "store"},
    "SYNC_STARTED":                 {"registered_phases", "run_id", "scope", "stores"},
    "BATTLENET_INSTALL_LAUNCH_REQUESTED": {"store_game_id"},
    "UBISOFT_INSTALL_LAUNCH_REQUESTED": {"store_game_id"},
}

# Events with a single owning subsystem, as a path prefix relative to
# ``py_modules/unifideck/``.
#
# The kwargs check above catches a second emitter that invents its own
# payload; it cannot catch one that copies the right payload from the wrong
# place. ``DownloadWorker`` is the sole dispatcher for all seven stores'
# ``install_game`` / ``update_game``, so it is the only thing that knows
# when a download really starts and really finishes — a store installer
# emitting DOWNLOAD_* is a duplicate by construction, and its "complete"
# fires before the worker has run prefix warmup. Audit item #4.
EMITTER_OWNERS: dict[str, str] = {
    f"DOWNLOAD_{name}": "services/download/"
    for name in (
        "QUEUED", "STARTED", "PROGRESS", "COMPLETE", "FAILED", "CANCELLED",
    )
}


def validate_schema_keys() -> int:
    """Assert every CANONICAL_SCHEMA key is a real Events member.

    Catches the failure mode that made the previous schema
    silently useless: declaring events (``AUTH_STARTED``,
    ``ARTWORK_READY``, …) that no longer exist in the enum, so
    the comparison never matched them and never complained.

    Returns the number of phantom keys (0 = all valid).
    """
    phantom = sorted(set(CANONICAL_SCHEMA) - VALID_EVENTS)
    for name in phantom:
        print(
            f"  ✗  CANONICAL_SCHEMA key {name!r} is not a "
            f"member of the Events enum"
        )
    return len(phantom)


def walk_sources(root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Merge SchemaExtractor results across every .py file.

    Extraction noise — any first-arg shape that isn't a literal
    ``Events.X`` / string the extractor can resolve to a real
    enum member — is dropped here. In particular the priority
    dispatcher's ``bus.emit(item.event, ...)`` resolves to the
    bogus name ``"event"``; filtering on VALID_EVENTS removes it
    along with any other variable / attribute re-emit.

    Returns two maps: ``event -> observed kwargs`` and
    ``event -> emitting file paths`` (relative to *root*, for
    :func:`check_emitter_owners`).
    """
    merged: dict[str, set[str]] = {}
    emitters: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        extracted = SchemaExtractor.extract_from_source(source)
        rel = path.relative_to(root).as_posix()
        for event, kwargs in extracted.items():
            if event not in VALID_EVENTS:
                # Extraction noise (variable/attribute re-emit
                # such as priority_dispatcher's item.event).
                continue
            merged.setdefault(event, set()).update(kwargs)
            emitters.setdefault(event, set()).add(rel)
    return merged, emitters


#: Subscriber payload reads that are deliberately tolerated rather than
#: honoured — a key read only as an ``or`` fallback beside a
#: schema-declared primary. These are dead branches, not defects: the
#: primary path is correct and no emitter has ever sent the fallback name.
#: Keyed ``"<module>::<handler>"`` so a rename surfaces here rather than
#: silently widening the exemption.
#:
#: Do NOT add a row to silence a real mismatch. The whole point of this
#: check is that ``rc``/``exit_code`` (audit correction C-2) sat unnoticed
#: for a release because nothing compared the two sides.
#: Empty, and that is the goal. Its two founding entries — the flat
#: ``games`` / ``is_force`` reads on ``POST_SYNC_PHASE_CHANGED`` — were
#: deleted once this check surfaced them (audit register item 41) rather than
#: carried. Prefer that: an exemption is a place for a defect to hide.
TOLERATED_SUBSCRIBER_READS: dict[str, set[str]] = {}


def walk_subscribers(root: Path) -> list[tuple[str, str, str, set[str]]]:
    """Collect every ``@subscribe``-decorated handler's payload reads.

    Returns ``(event, relative_path, handler_name, keys)`` per handler,
    where *keys* are the literal names passed to ``kwargs.get(...)``.

    This is the half :func:`compare` cannot see. ``validate_event_schemas``
    was emit-side only, so a handler reading a key no emitter sends was
    invisible — and that is the tree's most expensive recurring defect:
    ``GAME_INSTALLED`` shipped it (``app_id`` vs ``game_id``),
    ``TOAST_NOTIFICATION`` shipped it (``params`` vs ``i18n_params``), and
    ``GAME_STOPPED`` was still shipping it (``rc`` vs ``exit_code``) when
    this check was written. §1.1.1 of the audit states the rule in words —
    "read the subscriber's ``kwargs.get(...)`` keys against the emit site's
    kwargs" — and nothing enforced it.
    """
    found: list[tuple[str, str, str, set[str]]] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            event = _subscribed_event(node)
            if event is None:
                continue
            found.append((event, rel, node.name, _kwargs_get_keys(node)))
    return found


def _subscribed_event(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The ``Events.X`` member name in a ``@subscribe(...)`` decorator."""
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        target = dec.func
        name = getattr(target, "id", getattr(target, "attr", None))
        if name != "subscribe":
            continue
        for arg in dec.args:
            if isinstance(arg, ast.Attribute):
                return arg.attr
    return None


def _kwargs_get_keys(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Literal keys read via ``kwargs.get("...")`` inside *node*."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "kwargs"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            keys.add(sub.args[0].value)
    return keys


def check_subscriber_reads(
    subscribers: list[tuple[str, str, str, set[str]]],
) -> int:
    """Fail on a handler reading a key its event's schema does not declare.

    Only events with a ``CANONICAL_SCHEMA`` row are checked — an event with
    no declared contract has nothing to compare against, and
    :func:`compare` already reports it.
    """
    errors = 0
    tolerated = 0
    for event, rel, handler, keys in subscribers:
        declared = CANONICAL_SCHEMA.get(event)
        if not declared:
            continue
        unknown = keys - declared
        if not unknown:
            continue
        allowed = TOLERATED_SUBSCRIBER_READS.get(f"{rel}::{handler}", set())
        real = unknown - allowed
        tolerated += len(unknown & allowed)
        if not real:
            continue
        errors += 1
        print(
            f"  ✗  {event}: {rel}::{handler}() reads "
            f"{sorted(real)}, which no emitter sends"
        )
        print(f"       declared payload: {sorted(declared)}")
        print(
            "       Either the handler's key names are wrong, or the "
            "emitter never\n"
            "       sent what it promised. A silent no-op either way — "
            "see audit\n"
            "       §1.1.1. If it is a dead 'or' fallback beside a correct "
            "primary,\n"
            "       add it to TOLERATED_SUBSCRIBER_READS with a reason."
        )
    if tolerated:
        print(
            f"→ {tolerated} tolerated subscriber read(s) "
            f"(dead fallbacks, see TOLERATED_SUBSCRIBER_READS)"
        )
    return errors


def check_emitter_owners(emitters: dict[str, set[str]]) -> int:
    """Print emitters outside an event's owning subsystem.

    Prints the number of violations (0 = clean). An event with no
    observed emitter is skipped, same as :func:`compare` does.
    """
    errors = 0
    for event, owner in sorted(EMITTER_OWNERS.items()):
        for path in sorted(emitters.get(event, set())):
            if path.startswith(owner):
                continue
            print(
                f"  ✗  {event}: emitted from {path!r}, but only "
                f"{owner!r} may emit it (single-emitter event)"
            )
            errors += 1
    return errors


def compare(
    actual: dict[str, set[str]],
    canonical: dict[str, set[str]],
) -> int:
    """Print mismatches and return the error count.

    Two failure classes:
      * a declared event is emitted with a kwarg not in its
        allowed set (contract too narrow, or a typo at the
        emit site);
      * an event is emitted (and is a real enum member, since
        ``actual`` is already filtered) but has no entry in
        CANONICAL_SCHEMA.

    The historical ``_batch`` suffix is still skipped: the
    priority dispatcher synthesises ``"<event>_batch"`` names
    for coalesced delivery — these are transport-level, not
    part of the public event contract.
    """
    errors = 0
    for event, allowed in canonical.items():
        seen = actual.get(event)
        if seen is None:
            # Declared but not observed in source (e.g. emitted
            # only through a wrapper). Nothing to check.
            continue
        unexpected = seen - allowed
        if unexpected:
            print(
                f"  ✗  {event}: unexpected kwargs "
                f"{sorted(unexpected)} (allowed: {sorted(allowed)})"
            )
            errors += 1
    for event in sorted(actual.keys() - canonical.keys()):
        if event.endswith("_batch"):
            continue
        print(
            f"  ⚠  {event}: emitted but not in CANONICAL_SCHEMA"
        )
        errors += 1
    return errors


def main() -> int:
    target = ROOT / "py_modules" / "unifideck"
    if not target.is_dir():
        print(f"✗ source dir not found: {target}")
        return 2

    # Fail fast on a stale schema before doing any walking.
    phantom = validate_schema_keys()
    if phantom:
        print(
            f"\n✗ {phantom} CANONICAL_SCHEMA key(s) not in the "
            f"Events enum — update the schema"
        )
        return 1

    print(f"→ walking {target}")
    actual, emitters = walk_sources(target)
    print(
        f"→ extracted {len(actual)} distinct events from source "
        f"(noise filtered against {len(VALID_EVENTS)} enum members)"
    )
    errors = compare(actual, CANONICAL_SCHEMA)
    errors += check_emitter_owners(emitters)

    subscribers = walk_subscribers(target)
    print(
        f"→ checking {len(subscribers)} @subscribe handler(s) against the "
        f"declared payloads"
    )
    errors += check_subscriber_reads(subscribers)
    if errors == 0:
        print("\n✓ event schemas valid")
        return 0
    print(f"\n✗ {errors} schema error(s) found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
