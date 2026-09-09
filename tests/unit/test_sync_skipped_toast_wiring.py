"""SYNC_SKIPPED must reach the user, end to end across three artifacts.

Audit §1.3. ``MicrosoftStore`` skips the whole xCloud sync when the Game Pass
subscription probe returns NONE, an unknown tier, or an error. The event was
emitted, declared in ``src/types/events.ts`` and polled via ``WATCHED_EVENTS``
— and nothing subscribed, so the library silently failed to sync while the
sync bar reported success for the other five stores. The three explanatory
toast strings had been written and translated into all 16 locales the whole
time and were referenced by nothing.

The chain has three independently-breakable links, and breaking any one is
silent:

    backend `reason=` → SYNC_SKIPPED_KEYS in boot-event-listener → locale key

``validate_event_wiring.py`` covers the event's own wiring; it cannot see that
a NEW reason string has no row in the map, or that a row points at a key
nobody translated. That is what this pins.

Read as text rather than executed: ``boot-event-listener.tsx`` imports Decky
UI, i18next and a React modal, none of which resolve in the Python suite, and
the thing worth pinning is the agreement between three files.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from tests.unit._repo_root import find_repo_file

LISTENER = "src/services/boot-event-listener.tsx"
STORE = "py_modules/unifideck/stores/microsoft/microsoft_store.py"
EN_US = "src/i18n/locales/en-US.json"
LOCALES = "src/i18n/locales"


def _require(rel: str) -> Path:
    path = find_repo_file(rel)
    if path is None:
        pytest.skip(f"{rel} not found (set UNIFIDECK_REPO_ROOT)")
    return path


def _emitted_reasons() -> set[str]:
    """Every ``reason=`` MicrosoftStore passes to a SYNC_SKIPPED emit.

    AST-based: the emits span several lines each, so a line-oriented read
    would find the event name and miss the reason.
    """
    tree = ast.parse(_require(STORE).read_text(encoding="utf-8"))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or "emit" not in func.attr:
            continue
        first = node.args[0]
        is_skip = (
            isinstance(first, ast.Attribute)
            and first.attr == "SYNC_SKIPPED"
        ) or (
            isinstance(first, ast.Constant) and first.value == "sync_skipped"
        )
        if not is_skip:
            continue
        for kw in node.keywords:
            if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                reasons.add(str(kw.value.value))
    return reasons


def _mapped_reasons() -> dict[str, str]:
    """``reason -> i18n key`` as declared in SYNC_SKIPPED_KEYS."""
    text = _require(LISTENER).read_text(encoding="utf-8")
    block = re.search(
        r"SYNC_SKIPPED_KEYS\s*:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}",
        text, re.S,
    )
    assert block, "SYNC_SKIPPED_KEYS table not found in the boot event listener"
    return dict(
        re.findall(r"(\w+)\s*:\s*\"([\w.]+)\"", block.group(1)),
    )


def _lookup(bundle: dict, dotted: str) -> object | None:
    node: object = bundle
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_every_emitted_reason_has_a_toast() -> None:
    """A reason with no row is a silent skip — the original bug, exactly."""
    emitted = _emitted_reasons()
    assert emitted, "no SYNC_SKIPPED emits found — has the store moved?"
    missing = emitted - set(_mapped_reasons())
    assert not missing, (
        f"MicrosoftStore emits SYNC_SKIPPED with {sorted(missing)} but "
        f"SYNC_SKIPPED_KEYS has no row for it, so the user's xCloud library "
        f"silently fails to sync with no explanation"
    )


def test_the_map_has_no_rows_for_reasons_nobody_emits() -> None:
    """A stale row is a translated string kept alive for a dead code path."""
    stale = set(_mapped_reasons()) - _emitted_reasons()
    assert not stale, (
        f"SYNC_SKIPPED_KEYS maps {sorted(stale)}, which nothing emits")


def test_every_mapped_key_exists_in_english() -> None:
    """A key i18next cannot resolve renders as the key itself."""
    bundle = json.loads(_require(EN_US).read_text(encoding="utf-8"))
    for reason, key in sorted(_mapped_reasons().items()):
        value = _lookup(bundle, key)
        assert isinstance(value, str) and value.strip(), (
            f"reason {reason!r} maps to {key!r}, which is missing from "
            f"en-US.json")


def test_every_mapped_key_is_translated_everywhere() -> None:
    """The strings were already translated 16 times before anything used them.

    A later edit that adds a reason with an English-only key would ship a
    toast that reads in English to every non-English user, which is the kind
    of half-delivery this whole audit item is about.
    """
    locales_dir = _require(EN_US).parent
    keys = set(_mapped_reasons().values())
    gaps: list[str] = []
    for path in sorted(locales_dir.glob("*.json")):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for key in sorted(keys):
            value = _lookup(bundle, key)
            if not (isinstance(value, str) and value.strip()):
                gaps.append(f"{path.name}:{key}")
    assert not gaps, f"untranslated skip explanations: {gaps}"


def test_the_listener_filters_unknown_reasons_instead_of_toasting_them() -> None:
    """SYNC_SKIPPED is generic over stores; an unmapped reason must be quiet.

    Without the guard, a future subscription store's reason would render as
    the raw i18n key in a toast.
    """
    text = _require(LISTENER).read_text(encoding="utf-8")
    handler = re.search(
        r"subscribe\(\"sync_skipped\".*?\}\),", text, re.S,
    )
    assert handler, "no sync_skipped subscriber in the boot event listener"
    assert "if (!key) return;" in handler.group(0), (
        "the sync_skipped handler must bail on an unmapped reason")
