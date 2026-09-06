#!/usr/bin/env python3
"""scripts/validate_event_wiring.py — CI guard for event WIRING.

Sibling of ``validate_event_schemas.py``. That script asks "does this emit
site send the right kwargs, from the right subsystem"; it is emit-side only
and cannot see the failure this one exists for:

    an event with a subscriber and no emitter, or an emitter and no
    subscriber, is indistinguishable from a working one at every call site.

Audit §1.3 found eleven of those, and three of them had a full row in
``CANONICAL_SCHEMA`` with a kwargs contract and zero emitters, which is
precisely what made them read as wired. Two cost users real behaviour for a
release apiece: playtime was inflated by the whole of every suspend because
``SUSPEND``/``RESUME`` had no emitter, and a failed Game Pass check dropped the
entire xCloud library in silence because ``SYNC_SKIPPED`` had no subscriber
while its explanatory toast sat translated in all 16 locales.

Usage:
    python3 scripts/validate_event_wiring.py
    echo $?   # 0 = clean, 1 = unwired events, 2 = error

──────────────────────────────────────────────────────────────
What "wired" means
──────────────────────────────────────────────────────────────
Every ``Events`` member must have BOTH a producer and a consumer:

  producer  = a Python emit site, or membership in INDIRECT_EMITTERS
  consumer  = a Python ``@subscribe``/``bus.subscribe``, OR a complete
              frontend leg

A frontend leg is complete only when all three parts are present, because
missing any one of them is silent:

  1. a row in ``src/types/events.ts``      — else nothing can name it
  2. a row in ``WATCHED_EVENTS``           — else it is never polled for
  3. a subscribe/useEventBus call in src/  — else it is polled and dropped

``SUBSCRIPTION_DETECTED`` was missing 1 and 2 and 3; ``SYNC_SKIPPED`` had 1
and 2 and was missing only 3, and was just as invisible. Partial legs are
reported specifically so the diagnosis is not "somewhere in the frontend".

──────────────────────────────────────────────────────────────
Opting out
──────────────────────────────────────────────────────────────
Some events are legitimately half-wired for a while. Mark the enum member:

    # unwired: consumer deferred, see register 4a
    CIRCUIT_STATE_CHANGED = "circuit_state_changed"

The marker is scanned through the whole contiguous comment block above the
member, so the reason can span lines and sit inside a longer explanation. It
is named for exactly what the check tests, so it stays honest whichever half
is missing. The exemption count prints on every clean run: an allowlist that
can grow quietly is not a gate.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py_modules"))

from unifideck.core.types.events import Events  # noqa: E402

BACKEND = ROOT / "py_modules" / "unifideck"
FRONTEND = ROOT / "src"
EVENTS_PY = BACKEND / "core" / "types" / "events.py"
EVENTS_TS = FRONTEND / "types" / "events.ts"
EVENT_BUS_CLIENT = FRONTEND / "api" / "event-bus-client.ts"

# Events whose emit goes through a helper that takes the event NAME as a
# parameter, so no ``Events.X`` literal appears at the emit site. Each entry
# names the helper that does the emitting, so the claim can be re-checked
# rather than trusted. These are producers; they still need a consumer.
#
# Keep this list minimal. Every entry is a place the static check is blind,
# which is how the whole §1.3 defect class survived — do not add one to
# silence a finding without opening the named file and confirming the emit.
INDIRECT_EMITTERS: dict[str, str] = {
    # services/cloud_save/{sync,service}.py — _emit_down / _emit_up take the
    # event name as a string argument.
    "CLOUD_SYNC_DOWN_COMPLETE": "services/cloud_save/_emit_down",
    "CLOUD_SYNC_DOWN_FAILED": "services/cloud_save/_emit_down",
    "CLOUD_SYNC_UP_COMPLETE": "services/cloud_save/_emit_up",
    "CLOUD_SYNC_UP_FAILED": "services/cloud_save/_emit_up",
    # services/download/wrapper_signals.py maps a store id to the event NAME
    # and emits by lookup.
    "UBISOFT_INSTALL_LAUNCH_REQUESTED": "services/download/wrapper_signals",
    "BATTLENET_INSTALL_LAUNCH_REQUESTED": "services/download/wrapper_signals",
}

# Events the frontend subscribes to through a table rather than a literal, so
# no ``subscribe(Events.X)`` / ``subscribe("x")`` call site exists to find.
# Same shape as INDIRECT_EMITTERS and the same rule: each entry names the file
# that does the subscribing, so the claim can be re-checked. These still need
# their ``events.ts`` and ``WATCHED_EVENTS`` rows — those are checked normally.
INDIRECT_FRONTEND_SUBSCRIBERS: dict[str, str] = {
    # src/stores/download-store.ts — WRAPPER_INSTALL_LAUNCHERS is a
    # [event, launcher, label] table looped into EventBusClient.subscribe, so
    # adding EA App is one row rather than another handler.
    "UBISOFT_INSTALL_LAUNCH_REQUESTED": "stores/download-store.ts",
    "BATTLENET_INSTALL_LAUNCH_REQUESTED": "stores/download-store.ts",
}

# The SECURITY_* family is emitted entirely through ``audit_emitter._emit`` /
# ``_emit_security_event`` and consumed entirely by SecurityService's audit
# log, which reaches a human through the support bundle. Treating ~15 members
# individually would bury the signal, so the family is handled as one unit —
# the same carve-out ``validate_event_schemas.py`` documents for its own
# extractor.
SECURITY_PREFIX = "SECURITY_"

MARKER = re.compile(r"#\s*unwired:\s*(?P<reason>.+)")


def read(path: Path) -> str:
    """File text, or empty string when the file is absent."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def enum_members() -> dict[str, str]:
    """``NAME -> "value"`` for every member of the Events enum."""
    return {e.name: e.value for e in Events}


def exemptions() -> dict[str, str]:
    """``NAME -> reason`` for every member carrying an ``# unwired:`` marker.

    Scans the contiguous comment block above each assignment so a real
    explanation can span lines, matching the ``# no-frontend-caller:`` marker
    convention ``validate_architecture.py`` already uses for dead RPCs.
    """
    lines = read(EVENTS_PY).splitlines()
    found: dict[str, str] = {}
    for i, line in enumerate(lines):
        match = re.match(r"\s{4}([A-Z][A-Z0-9_]*)\s*=\s*\"[a-z0-9_]+\"", line)
        if not match:
            continue
        for j in range(i - 1, -1, -1):
            stripped = lines[j].strip()
            if not stripped.startswith("#"):
                break
            marker = MARKER.search(stripped)
            if marker:
                found[match.group(1)] = marker.group("reason").strip()
                break
    return found


def _emitted_names(tree: ast.AST) -> set[str]:
    """Event names passed as the first arg of any ``.emit``-ish call."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or "emit" not in func.attr:
            continue
        first = node.args[0]
        # ``Events.X``
        if isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name):
            if first.value.id == "Events":
                names.add(first.attr)
        # ``"x"`` — the string form the bus also accepts.
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value.upper())
    return names


def _subscribed_names(tree: ast.AST) -> set[str]:
    """Event names passed as the first arg of any ``subscribe``-ish call.

    Covers both ``@subscribe(Events.X)`` decorators and ``bus.subscribe(...)``
    / ``bus.on(...)`` calls, since the decorator IS a call node.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            attr = func.attr
        elif isinstance(func, ast.Name):
            attr = func.id
        else:
            continue
        if attr not in {"subscribe", "on", "subscribe_replay"}:
            continue
        first = node.args[0]
        if isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name):
            if first.value.id == "Events":
                names.add(first.attr)
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value.upper())
    return names


def walk_backend() -> tuple[set[str], set[str]]:
    """``(emitted, subscribed)`` across every first-party backend module.

    AST-based rather than regex: an emit whose first argument sits on the next
    line (the dominant style in this tree) is invisible to a line-oriented
    grep, and reporting those as unwired would train people to ignore the gate.
    """
    emitted: set[str] = set()
    subscribed: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(read(path))
        except SyntaxError:
            continue
        emitted |= _emitted_names(tree)
        subscribed |= _subscribed_names(tree)
    return emitted, subscribed


def frontend_legs(value: str, name: str) -> tuple[bool, bool, bool]:
    """``(declared, watched, subscribed)`` for one event on the frontend.

    All three must hold for the frontend to be a real consumer. Missing any
    one is silent, and each fails differently, so they are reported apart.
    """
    declared = re.search(rf"\b{name}\s*:\s*\"{value}\"", read(EVENTS_TS)) is not None
    watched = f'"{value}"' in _watched_block()
    pattern = re.compile(
        rf"(?:useEventBus|subscribe(?:Replay)?)\s*\(\s*"
        rf"(?:Events\.{name}\b|\"{value}\")",
    )
    subscribed = name in INDIRECT_FRONTEND_SUBSCRIBERS
    for ext in ("*.ts", "*.tsx"):
        if subscribed:
            break
        for path in FRONTEND.rglob(ext):
            if path.name.endswith((".test.ts", ".test.tsx")):
                # A test subscribing to an event is not a consumer.
                continue
            if pattern.search(read(path)):
                subscribed = True
                break
    return declared, watched, subscribed


_WATCHED_CACHE: str | None = None


def _watched_block() -> str:
    """The ``WATCHED_EVENTS`` array literal, isolated from the rest of the file.

    Isolated deliberately: ``event-bus-client.ts`` also holds
    ``IMPERATIVE_EVENTS`` and ``STALE_ON_RELOAD_EVENTS``, and a naive
    whole-file search would let membership in either of those vouch for an
    event that is never actually polled — the same self-vouching hole
    ``validate_architecture.py`` had to close for ``rpc-routes.ts``.
    """
    global _WATCHED_CACHE
    if _WATCHED_CACHE is None:
        text = read(EVENT_BUS_CLIENT)
        match = re.search(
            r"WATCHED_EVENTS\s*:\s*EventName\[\]\s*=\s*\[(.*?)\]", text, re.S,
        )
        _WATCHED_CACHE = match.group(1) if match else ""
    return _WATCHED_CACHE


def main() -> int:
    if not EVENTS_TS.is_file() or not EVENT_BUS_CLIENT.is_file():
        print("✗ frontend event tables not found — cannot check wiring")
        return 2
    if not _watched_block():
        print(f"✗ could not locate WATCHED_EVENTS in {EVENT_BUS_CLIENT}")
        return 2

    members = enum_members()
    exempt = exemptions()
    emitted, subscribed = walk_backend()

    stale = sorted(set(exempt) - set(members))
    for name in stale:
        print(f"  ✗  '# unwired:' marker on {name!r}, which is not an Events member")

    print(f"→ {len(members)} events · {len(emitted)} emitted · {len(subscribed)} subscribed")

    errors = len(stale)
    for name, value in sorted(members.items()):
        if name.startswith(SECURITY_PREFIX):
            continue
        has_producer = name in emitted or name in INDIRECT_EMITTERS
        declared, watched, fe_sub = frontend_legs(value, name)
        has_consumer = name in subscribed or (declared and watched and fe_sub)
        if has_producer and has_consumer:
            continue
        if name in exempt:
            continue

        if not has_producer and not has_consumer:
            problem = "no emitter and no consumer — dead in both directions"
        elif not has_producer:
            problem = "subscribed but never emitted — the handler can never run"
        elif declared or watched or fe_sub:
            missing = [
                part for part, ok in (
                    ("a row in src/types/events.ts", declared),
                    ("a row in WATCHED_EVENTS", watched),
                    ("a subscribe/useEventBus call in src/", fe_sub),
                ) if not ok
            ]
            problem = (
                "emitted, and the frontend leg is incomplete — missing "
                + ", ".join(missing)
            )
        else:
            problem = "emitted but nothing consumes it on either leg"

        print(f"  ✗  {name}: {problem}")
        errors += 1

    if errors:
        print(
            f"\n✗ {errors} unwired event(s). Wire the missing half, retire the "
            f"event, or mark the member '# unwired: <reason>' if it is "
            f"deliberately deferred."
        )
        return 1

    print(f"\n✓ event wiring valid ({len(exempt)} deliberate exemption(s))")
    for name in sorted(exempt):
        print(f"    · {name}: {exempt[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
