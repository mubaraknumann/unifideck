#!/usr/bin/env python3
"""scripts/check_toast_length.py — Hold every toast string to the toast budget.

Why
---
A Decky toast shows its title and its body on one line each, cut with an
ellipsis at 276px. Toast text is held to a budget well inside that, per
field (title, body and action label are counted separately):

* 16 characters in every locale but Chinese, Japanese and Korean;
* 22 width units in those, where a CJK character is one unit and anything
  else 0.6. A CJK glyph is about twice as wide as a Latin one, so the
  budget is a width, not a count.

The frontend applies the same model at render time (``clampToastField`` in
``src/services/toast-text.ts``); the two must stay in step.

What is checked
---------------
**Locales.** Every ``toasts.*`` value, in every locale:

* its width fits the budget. A bounded placeholder (a number, a size, a
  language) is counted at its declared maximum width (``BOUNDED``);
* at most one unbounded placeholder (a game title, a store name), and only as
  the last thing in the string, after at most ``budget - RESERVE`` of text.
  The renderer cuts the line at the budget, so the start of the name stays
  visible;
* no raw-text placeholder (``RAW``). An error string or a path does not fit
  a toast; it becomes a short reason key, and the detail goes to the log.

**Headers.** A toast's title is its header: the feature's name, shared by
every toast about that feature (``toasts.header.*``), over a body that says
what happened. Every backend toast that names an ``i18n_key`` must also name
an ``i18n_title_key``, and that key must be a ``toasts.header.*`` key.

**Backend.** Every ``i18n_key`` / ``i18n_title_key`` / ``i18n_label_key``
that ``py_modules`` passes as a keyword or a dict entry, when it is a string
literal, a module constant or an f-string, must name a ``toasts.*`` key, and
every ``"toasts.…"`` literal in ``py_modules`` must exist in en-US. The
frontend side needs no scan: a toast title only accepts a ``ToastHeader``
(``headerText(key: `toasts.header.${string}`)``) and a body a
``ToastString`` (``toastText(key: `toasts.${string}`)``), so ``tsc`` holds
it.

Usage
-----
    python3 scripts/check_toast_length.py [--warn-only]

Exit codes: 0 clean (or ``--warn-only``), 1 findings, 2 could not run.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = REPO_ROOT / "src" / "i18n" / "locales"
BACKEND_ROOT = REPO_ROOT / "py_modules" / "unifideck"
SOURCE_LOCALE = "en-US"
NAMESPACE = "toasts."

FIELD_CHARS = 16
FIELD_UNITS = 22.0
NARROW_UNIT = 0.6
#: Room an unbounded placeholder keeps: five characters and the ellipsis.
RESERVE = 6.0
WIDE_LOCALES = frozenset({"ja-JP", "ko-KR", "zh-CN", "zh-TW"})

# Kept identical to WIDE_CHAR in src/services/toast-text.ts.
WIDE_RANGES = (
    (0x1100, 0x11FF), (0x3000, 0x303F), (0x3040, 0x309F), (0x30A0, 0x30FF),
    (0x3130, 0x318F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF), (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
)

#: Placeholders with a known longest value, in Latin characters.
BOUNDED = {
    "count": 4, "games": 5, "failures": 4, "attempt": 2, "max": 2, "seconds": 3,
    "size": 8, "free": 8,
    "need": 8, "language": 10, "compat": 10,
}
#: Placeholders that carry raw text. Never in a toast.
RAW = frozenset({"error", "error_code", "code", "path", "reason", "log"})

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*(?:,[^}]*)?\}\}")
KEY_FIELDS = frozenset({"i18n_key", "i18n_title_key", "i18n_label_key"})
HEADER_NAMESPACE = "toasts.header."
#: Calls that emit a toast and take the body key as their first argument.
POSITIONAL_EMITTERS = frozenset({"launcher_toast"})
TOAST_LITERAL = re.compile(r"^toasts\.[A-Za-z0-9_.]+$")


@dataclass(frozen=True)
class Finding:
    where: str
    message: str


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in WIDE_RANGES)


def _graphemes(text: str) -> list[str]:
    """Approximate grapheme clusters: marks and joiners attach to the base."""
    clusters: list[str] = []
    for ch in text:
        joins = unicodedata.combining(ch) or ch in "‍️︎"
        if clusters and joins:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def width(text: str, locale: str) -> float:
    """Width of ``text`` in the locale's unit: characters, or CJK units."""
    clusters = _graphemes(text)
    if locale not in WIDE_LOCALES:
        return float(len(clusters))
    return sum(1.0 if _is_wide(c[0]) else NARROW_UNIT for c in clusters)


def budget(locale: str) -> float:
    return FIELD_UNITS if locale in WIDE_LOCALES else float(FIELD_CHARS)


def _bounded_width(name: str, locale: str) -> float:
    chars = BOUNDED[name]
    return chars * (NARROW_UNIT if locale in WIDE_LOCALES else 1.0)


def check_value(key: str, value: str, locale: str) -> list[str]:
    """Problems with one toast string, as messages. Empty when it fits."""
    cap = budget(locale)
    slots = list(PLACEHOLDER.finditer(value))
    raw = sorted({m.group(1) for m in slots if m.group(1) in RAW})
    if raw:
        return [f"raw-text placeholder {raw}: use a short reason key"]

    unbounded = [m for m in slots if m.group(1) not in BOUNDED]
    if len(unbounded) > 1:
        names = [m.group(1) for m in unbounded]
        return [f"{len(unbounded)} unbounded placeholders {names}: one at most"]

    fixed = PLACEHOLDER.sub("", value)
    used = width(fixed, locale) + sum(
        _bounded_width(m.group(1), locale) for m in slots if m.group(1) in BOUNDED
    )
    if unbounded:
        slot = unbounded[0]
        if value[slot.end():].strip():
            return [f"{{{{{slot.group(1)}}}}} must be last"]
        room = cap - RESERVE
        if used > room:
            return [f"text before {{{{{slot.group(1)}}}}} is {used:g} wide, max {room:g}"]
        return []
    if used > cap:
        return [f"{used:g} wide, max {cap:g}"]
    return []


def flatten(obj: object, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            out.update(flatten(v, path))
    elif isinstance(obj, str):
        out[prefix] = obj
    return out


def load_locales() -> dict[str, dict[str, str]]:
    locales: dict[str, dict[str, str]] = {}
    for path in sorted(LOCALES_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            locales[path.stem] = flatten(json.load(handle))
    return locales


def check_locales(locales: dict[str, dict[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for locale, strings in locales.items():
        for key, value in strings.items():
            if not key.startswith(NAMESPACE) or key.endswith("_comment"):
                continue
            for message in check_value(key, value, locale):
                findings.append(Finding(f"{locale} {key}", f"{message}: {value!r}"))
    return findings


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    return constants


def _resolve(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The key ``node`` names (an f-string yields its fixed prefix), or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _key_nodes(tree: ast.Module) -> list[ast.expr]:
    nodes: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            nodes.extend(kw.value for kw in node.keywords if kw.arg in KEY_FIELDS)
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values, strict=True):
                if isinstance(k, ast.Constant) and k.value in KEY_FIELDS:
                    nodes.append(v)
    return nodes


def _toast_fields(node: ast.AST) -> dict[str, ast.expr] | None:
    """The key fields of a call or dict literal that emits a toast, or None."""
    fields: dict[str, ast.expr] = {}
    if isinstance(node, ast.Call):
        fields = {kw.arg: kw.value for kw in node.keywords if kw.arg in KEY_FIELDS}
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        if name in POSITIONAL_EMITTERS and node.args:
            fields.setdefault("i18n_key", node.args[0])
    elif isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values, strict=True):
            if isinstance(k, ast.Constant) and k.value in KEY_FIELDS:
                fields[str(k.value)] = v
    return fields if "i18n_key" in fields else None


def _header_problem(fields: dict[str, ast.expr], constants: dict[str, str]) -> str | None:
    """Why a toast emit breaks the header rule, or None.

    A body passed on as a bare name is a payload builder forwarding its
    caller's keys; the caller is checked instead.
    """
    body = fields["i18n_key"]
    if isinstance(body, ast.Name) and body.id not in constants:
        return None
    title = fields.get("i18n_title_key")
    if title is None:
        return 'toast without a header: pass i18n_title_key="toasts.header.…"'
    key = _resolve(title, constants)
    if key is not None and not key.startswith(HEADER_NAMESPACE):
        return f"header {key!r} is not a toasts.header.* key"
    return None


def check_backend(source_keys: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = str(path.relative_to(REPO_ROOT))
        constants = _module_constants(tree)
        for node in _key_nodes(tree):
            key = _resolve(node, constants)
            if key is not None and not key.startswith(NAMESPACE):
                findings.append(Finding(f"{where}:{node.lineno}", f"toast key outside toasts.*: {key!r}"))
        for node in ast.walk(tree):
            fields = _toast_fields(node)
            problem = _header_problem(fields, constants) if fields else None
            if problem:
                findings.append(Finding(f"{where}:{getattr(node, 'lineno', 0)}", problem))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and TOAST_LITERAL.match(node.value) and not node.value.endswith(".")
                    and node.value not in source_keys):
                findings.append(Finding(f"{where}:{node.lineno}", f"no such key in {SOURCE_LOCALE}: {node.value!r}"))
    return findings


def main(argv: list[str]) -> int:
    warn_only = "--warn-only" in argv
    try:
        locales = load_locales()
    except (OSError, ValueError) as exc:
        print(f"[check_toast_length] error: could not read locales: {exc}", file=sys.stderr)
        return 2
    if SOURCE_LOCALE not in locales:
        print(f"[check_toast_length] error: no {SOURCE_LOCALE} locale", file=sys.stderr)
        return 2

    findings = check_locales(locales) + check_backend(set(locales[SOURCE_LOCALE]))
    if not findings:
        print("[check_toast_length] OK — every toast string fits the toast budget.")
        return 0
    for finding in findings:
        print(f"  {finding.where}: {finding.message}")
    print(
        f"[check_toast_length] {'WARN' if warn_only else 'FAIL'} — {len(findings)} finding(s). "
        f"A toast field holds {FIELD_CHARS} characters ({FIELD_UNITS:g} width units in "
        f"{', '.join(sorted(WIDE_LOCALES))}); split a long message across title and body.",
    )
    return 0 if warn_only else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
