#!/usr/bin/env python3
"""scripts/check_hardcoded_strings.py — Catch user-facing English with no key.

The blind spot this closes
--------------------------
``check_orphan_keys.py`` walks from a key to its translations, and
``check_untranslated.py`` walks from a translation to its value. Neither can
see a string that never became a key at all — a toast built from a raw
template literal is invisible to both, and to every other gate in the repo.

Toasts are the surface that matters here. They are the plugin's main channel
for telling the user something went wrong, they are emitted from three
different layers (frontend hooks, backend services, and the out-of-process
launcher), and they are the one place a hardcoded string reaches a user in
their own language settings without anything failing.

What is checked
---------------
**Frontend** (``src/**/*.{ts,tsx}``) — at every ``toast.success|error|warn|info(``,
``showToast(`` and ``toaster.toast(`` call, any string or template literal in
the argument list must come from i18next. These forms are accepted:

* the key argument of ``t(...)`` / ``i18n.t(...)``;
* a ``defaultValue:`` option (``t(key, { defaultValue: "…" })`` is a legitimate
  in-place fallback and ``PluginUpdater.tsx`` uses it throughout);
* a severity token (``"info"`` / ``"warning"`` / ``"error"``), which is a union
  type argument rather than prose;
* a template that already interpolates ``t(...)``.

**Backend** (``py_modules/unifideck/**/*.py``) — every ``launcher_toast(...)``,
``emit_stage(...)`` and ``bus.emit(Events.LAUNCHER_STAGE, ...)`` call must carry
an ``i18n_key`` or ``i18n_title_key``, and no ``label`` / ``title`` / ``body``
inside a toast payload may be literal prose. (``TOAST_NOTIFICATION`` was also
matched here until it was retired in 2026-08; ``LAUNCHER_STAGE`` is now the only
toast channel.)

Deliberately NOT checked: ``phase_message``. The wrapper-install watcher and
the GOG installer build English progress text, but ``DownloadProgressRow.tsx``
ignores it on purpose and renders localized text from the phase instead, so
those strings are log-facing. See the comment in that component.

Opting out
----------
Add an ``i18n-ignore`` marker comment on the offending line, mirroring the
``rtl-ignore`` convention in ``lint_rtl.py`` and the repo's ``# noqa`` habit.

Usage
-----
    python3 scripts/check_hardcoded_strings.py

Exit codes: 0 clean, 1 findings, 2 could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
PY_ROOT = REPO_ROOT / "py_modules" / "unifideck"

IGNORE_MARKER = "i18n-ignore"
MAX_CALL_SPAN = 1500  # generous: the longest real toast call is ~20 lines

# ── frontend ────────────────────────────────────────────────
TOAST_CALL = re.compile(
    r"\b(?:toast\.(?:success|error|warn|info)|showToast|toaster\.toast)\s*\(",
)
STRING_LITERAL = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^']*'")
# The literal is the key argument of t(...) / i18n.t(...).
T_CALL_BEFORE = re.compile(r"\b(?:i18n\.)?t\(\s*$")
DEFAULT_VALUE_BEFORE = re.compile(r"defaultValue\s*:\s*$")
SEVERITY_TOKENS = frozenset({"info", "warning", "warn", "error", "success"})
# ``${...}`` holds a value, not prose. A template assembled only from
# interpolations and punctuation carries no English of its own, so its parts
# are already localized wherever they were built:
# ``${typeLabel} v${displayVersion}...`` is fine, ``Starting ${store} …`` is not.
TEMPLATE_SLOT = re.compile(r"\$\{[^}]*\}")

# ── backend ─────────────────────────────────────────────────
PY_TOAST_CALL = re.compile(
    r"(?<!def )\b(?:launcher_toast|emit_stage)\s*\(",
)
# ``bus.emit(Events.X, **payload)`` hides its keys in a dict built earlier, so
# the key check cannot see them. Skipping is correct rather than lenient: the
# payload's own construction site is itself a checked call.
PY_DICT_SPLAT = re.compile(r"\*\*\w+")
PY_BUS_EMIT = re.compile(
    r"\bemit\(\s*Events\.LAUNCHER_STAGE\b",
)
PY_LABEL_TEXT = re.compile(
    r"[\"']?\b(?:label|title|body)[\"']?\s*[:=]\s*[\"']([^\"']{4,})[\"']",
)
PROSE = re.compile(r"[A-Za-z]{3}")


def call_span(text: str, open_paren: int) -> str:
    """Return the argument text of the call whose ``(`` is at ``open_paren``."""
    depth = 0
    for index in range(open_paren, min(len(text), open_paren + MAX_CALL_SPAN)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:index]
    return text[open_paren + 1:open_paren + MAX_CALL_SPAN]


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def line_text(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return text[start:end if end != -1 else len(text)]


def iter_source(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.suffix in suffixes
        and ".test." not in p.name
        and "i18n/locales" not in p.as_posix()
    )


def check_frontend() -> list[tuple[Path, int, str]]:
    """Literals reaching a toast without going through i18next."""
    findings: list[tuple[Path, int, str]] = []
    for path in iter_source(SRC_ROOT, (".ts", ".tsx")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for call in TOAST_CALL.finditer(text):
            open_paren = call.end() - 1
            args = call_span(text, open_paren)
            for literal in STRING_LITERAL.finditer(args):
                raw = literal.group(0)
                inner = raw[1:-1]
                if raw.startswith("`"):
                    inner = TEMPLATE_SLOT.sub(" ", inner)
                if not PROSE.search(inner):
                    continue
                if inner in SEVERITY_TOKENS:
                    continue
                if "t(" in inner:  # template interpolating t(...)
                    continue
                before = args[max(0, literal.start() - 60):literal.start()]
                if T_CALL_BEFORE.search(before):
                    continue
                if DEFAULT_VALUE_BEFORE.search(before):
                    continue
                offset = open_paren + 1 + literal.start()
                if IGNORE_MARKER in line_text(text, offset):
                    continue
                findings.append(
                    (path, line_of(text, offset), raw[:80]),
                )
    return findings


def check_backend() -> list[tuple[Path, int, str]]:
    """Toast emissions with no i18n key, or literal prose in a toast payload."""
    findings: list[tuple[Path, int, str]] = []
    for path in iter_source(PY_ROOT, (".py",)):
        text = path.read_text(encoding="utf-8", errors="ignore")

        spans: list[tuple[int, str]] = []
        for call in PY_TOAST_CALL.finditer(text):
            open_paren = call.end() - 1
            spans.append((open_paren, call_span(text, open_paren)))
        for emit in PY_BUS_EMIT.finditer(text):
            open_paren = text.find("(", emit.start())
            if open_paren != -1:
                spans.append((open_paren, call_span(text, open_paren)))

        for open_paren, args in spans:
            line = line_of(text, open_paren)
            if IGNORE_MARKER in line_text(text, open_paren):
                continue
            has_key = "i18n_key" in args or "i18n_title_key" in args
            if not has_key and not PY_DICT_SPLAT.search(args):
                findings.append(
                    (path, line, "toast emitted with no i18n_key"),
                )
            for label in PY_LABEL_TEXT.finditer(args):
                value = label.group(1)
                if not PROSE.search(value):
                    continue
                offset = open_paren + 1 + label.start()
                if IGNORE_MARKER in line_text(text, offset):
                    continue
                findings.append(
                    (path, line_of(text, offset), f"literal toast text {value!r}"),
                )
    return findings


def main() -> int:
    if not SRC_ROOT.is_dir() or not PY_ROOT.is_dir():
        print(
            "[check_hardcoded_strings] error: run from the repo root",
            file=sys.stderr,
        )
        return 2

    frontend = check_frontend()
    backend = check_backend()
    if not frontend and not backend:
        print(
            "[check_hardcoded_strings] OK — every toast string resolves "
            "through i18next.",
        )
        return 0

    print(
        f"[check_hardcoded_strings] FAIL — {len(frontend) + len(backend)} "
        "hardcoded user-facing string(s). Add a translation key, or mark the "
        f"line {IGNORE_MARKER} with a reason:",
        file=sys.stderr,
    )
    for title, findings in (("frontend", frontend), ("backend", backend)):
        if not findings:
            continue
        print(f"\n[{title}]", file=sys.stderr)
        for path, line, detail in findings:
            rel = path.relative_to(REPO_ROOT)
            print(f"  {rel}:{line}  {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
