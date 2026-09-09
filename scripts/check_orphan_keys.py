#!/usr/bin/env python3
"""scripts/check_orphan_keys.py — Validate translation-key coverage.

Four independent checks, any of which can fail the run:

1. Orphan check — literal keys used in the codebase (``t("key")`` /
   ``i18nKey="key"``) that are NOT declared in a locale file.

2. Completeness check — keys declared in the en-US source of truth that are
   MISSING from another locale (so i18next silently falls back to English).
   This catches keys the orphan scan cannot see because they reach ``t()``
   indirectly via a helper (e.g. ``t(statusLabelKey(...))``) rather than as a
   string literal.

3. Unreferenced check — keys DECLARED in every locale that nothing anywhere
   reaches. This is the reverse direction of check 1, and it was missing.

   Checks 1 and 2 both run code → locale. Nothing ran locale → code, and that
   is the single most repeated defect in the 2026-08 architecture audit: a
   feature's strings get written and translated into all 16 locales while its
   delivery is never built, so the gate stays green and the user sees nothing.
   It happened to ``TOAST_NOTIFICATION`` (§1.1.2 — "the i18n strings existed
   and were translated in all 16 locales the whole time; only the delivery
   channel was dead"), to the three ``SYNC_SKIPPED`` explanations that would
   have told Game Pass users why their library vanished, to four
   ``errors.download.*`` codes, to ``toasts.storeError``, and to
   ``microsoft.subscriptionDetected``. Every one would have shown up here.

   A key counts as referenced if its full dotted form appears anywhere in
   ``src/``, ``py_modules/unifideck/`` or ``bin/`` — the backend sends
   ``i18n_key`` strings, so Python is part of the haystack — or if its last
   segment appears, which tolerates runtime composition such as
   ``t(`errors.download.${code}`)``. That is the generous test on purpose:
   a false positive here costs a real translated string.

   The already-dead keys are grandfathered in ``i18n_unused_baseline.json``,
   which may only ever SHRINK — the same rule the volumetry allowlists use.
   A newly-dead key fails immediately; a baseline key that becomes reachable
   prints a reminder to drop it from the baseline.

4. Backend-key check — an ``i18n_key=`` the **backend** names that no locale
   declares. Check 1 only scans ``t("key")`` in ``src/``, so the 48 literal
   ``i18n_key=`` arguments in ``py_modules/`` were checked from neither
   direction. i18next prints the key itself when it is missing, so the cost
   of a typo is the user reading ``toasts.launcher.errorNetwork`` verbatim.

Exits non-zero if any check finds a problem.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
LOCALES_DIR = SRC_ROOT / "i18n" / "locales"
SOURCE_LOCALE = "en-US"

#: Grandfathered unreferenced keys (check 3). Shrink-only.
UNUSED_BASELINE = Path(__file__).resolve().parent / "i18n_unused_baseline.json"

#: Where a key can be reached from. Python is in here because the backend
#: emits ``i18n_key`` strings that the frontend renders — a key can be live
#: and never appear in a single ``.tsx`` file.
REFERENCE_ROOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("src", (".ts", ".tsx")),
    ("py_modules/unifideck", (".py",)),
    ("bin", ()),
)


def _reference_haystack() -> str:
    """Every file a translation key could be named in, concatenated."""
    chunks: list[str] = []
    for rel, suffixes in REFERENCE_ROOTS:
        root = REPO_ROOT / rel
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if "i18n/locales" in path.as_posix() or "__pycache__" in path.parts:
                continue
            if suffixes and path.suffix not in suffixes:
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return "\n".join(chunks)


def find_unreferenced_keys(source_keys: set[str]) -> list[str]:
    """Declared keys that nothing anywhere reaches.

    Generous by design: a key survives if its full dotted form OR its last
    segment appears in the haystack, so runtime-composed keys such as
    ``t(`errors.download.${code}`)`` are not reported.
    """
    haystack = _reference_haystack()
    unreferenced = []
    for key in sorted(source_keys):
        leaf = key.rsplit(".", 1)[-1]
        if key in haystack or leaf in haystack:
            continue
        unreferenced.append(key)
    return unreferenced


#: Keyword arguments whose literal value is an i18n key the frontend renders.
#: The backend sends these over the bus (``LAUNCHER_STAGE``) and i18next
#: falls back to printing the key itself when it is missing — so a typo here
#: shows the user ``toasts.launcher.errorNetwork`` verbatim.
BACKEND_I18N_KWARGS = ("i18n_key", "i18n_title_key")


def find_backend_keys_without_a_string(declared: set[str]) -> list[tuple[str, str]]:
    """Backend-named i18n keys that no locale declares.

    Check 1 runs ``t("key")`` in ``src/`` against the locales, so a key the
    **backend** names has never been checked from either direction. That
    matters because the backend is a first-class producer of user-facing
    strings: 48 literal ``i18n_key=`` arguments live in ``py_modules/``.

    The class is real. ``ExitCode.user_message_key`` mapped nine exit codes
    to ``toasts.launcher.*`` keys of which **eight were never written into
    any locale** — the inverse of the audit's usual finding (§1.1.2, where
    the strings existed and only the delivery was dead). It went unnoticed
    because the function also had no callers; had anyone wired it, users
    would have seen raw key names.

    Scoped to the two kwargs rather than any dotted string on purpose: a
    first cut matching every ``"a.b.c"`` literal reported 9 false positives,
    all of them config keys and filenames (``sync.cooldown_seconds``,
    ``library.json``).
    """
    import ast

    root = REPO_ROOT / "py_modules" / "unifideck"
    missing: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg in BACKEND_I18N_KWARGS
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                    and kw.value.value not in declared
                ):
                    missing.append((
                        kw.value.value,
                        f"{path.relative_to(REPO_ROOT)}:{kw.value.lineno}",
                    ))
    return missing


def _load_unused_baseline() -> set[str]:
    if not UNUSED_BASELINE.is_file():
        return set()
    try:
        data = json.loads(UNUSED_BASELINE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("keys", []))

# Regexes to capture literal string keys in translation calls.
# 1. t("key") or t('key') or t(`key`)
T_REGEX = re.compile(r"\bt\(\s*(?:'([^']+)'|\"([^\"]+)\"|`([^`]+)`)\s*")
# 2. i18nKey="key" or i18nKey='key' or i18nKey={"key"}
I18NKEY_REGEX = re.compile(r"\bi18nKey\s*=\s*(?:['\"]([^'\"]+)['\"]|\{\s*(?:'([^']+)'|\"([^\"]+)\")\s*\})")


def flatten_json(obj: object, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if not isinstance(obj, dict):
        return flat
    for key, value in obj.items():
        composed = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_json(value, composed))
        elif isinstance(value, str):
            flat[composed] = value
    return flat


def scan_frontend_files() -> dict[str, list[tuple[Path, int]]]:
    """Scan all .ts and .tsx files in src/ and return a map of {key: [(file_path, line_no), ...]}."""
    used_keys: dict[str, list[tuple[Path, int]]] = {}

    for p in SRC_ROOT.rglob("*"):
        if p.suffix not in (".ts", ".tsx"):
            continue
        # Skip locales directory to avoid scanning translation files themselves
        if "i18n/locales" in p.as_posix():
            continue

        try:
            content = p.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[check_orphan_keys] warning: could not read {p}: {e}", file=sys.stderr)
            continue

        for line_idx, line in enumerate(content.splitlines(), start=1):
            # Scan for t(...)
            for match in T_REGEX.finditer(line):
                # Extract first non-empty group
                key = next((g for g in match.groups() if g is not None), None)
                if key and not "${" in key and not "+" in key:
                    used_keys.setdefault(key, []).append((p, line_idx))

            # Scan for i18nKey=...
            for match in I18NKEY_REGEX.finditer(line):
                key = next((g for g in match.groups() if g is not None), None)
                if key and not "${" in key and not "+" in key:
                    used_keys.setdefault(key, []).append((p, line_idx))

    return used_keys


def main() -> int:
    locale_files = sorted(LOCALES_DIR.glob("*.json"))
    if not locale_files:
        print(f"[check_orphan_keys] error: no locale files found in {LOCALES_DIR}", file=sys.stderr)
        return 2

    # Parse every locale once: {locale_name: {flat_key: value}}.
    flat_by_locale: dict[str, dict[str, str]] = {}
    for path in locale_files:
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[check_orphan_keys] error: failed to parse {path}: {e}", file=sys.stderr)
            return 2
        flat_by_locale[path.stem] = flatten_json(data)

    if SOURCE_LOCALE not in flat_by_locale:
        print(f"[check_orphan_keys] error: source locale {SOURCE_LOCALE}.json not found", file=sys.stderr)
        return 2

    used_keys_map = scan_frontend_files()
    used_keys = set(used_keys_map.keys())

    # --- Check 1: orphan keys (used in code, undeclared in a locale) ---
    locale_orphans: dict[str, list[str]] = {}
    for locale_name, flat_data in flat_by_locale.items():
        declared_keys = set(flat_data.keys())
        missing = sorted(
            k for k in used_keys
            if k not in declared_keys and not k.endswith("._comment")
        )
        if missing:
            locale_orphans[locale_name] = missing

    # --- Check 2: completeness vs en-US source of truth ---
    source_keys = {
        k for k in flat_by_locale[SOURCE_LOCALE] if not k.endswith("._comment")
    }
    locale_incomplete: dict[str, list[str]] = {}
    for locale_name, flat_data in flat_by_locale.items():
        if locale_name == SOURCE_LOCALE:
            continue
        missing = sorted(source_keys - set(flat_data.keys()))
        if missing:
            locale_incomplete[locale_name] = missing

    # --- Check 4: backend-named keys with no string behind them ---
    backend_missing = find_backend_keys_without_a_string(source_keys)

    # --- Check 3: declared keys nothing reaches (locale -> code) ---
    baseline = _load_unused_baseline()
    unreferenced = find_unreferenced_keys(source_keys)
    newly_dead = sorted(set(unreferenced) - baseline)
    revived = sorted(baseline - set(unreferenced) - (baseline - source_keys))
    stale_baseline = sorted(baseline - source_keys)

    if (
        not locale_orphans
        and not locale_incomplete
        and not newly_dead
        and not backend_missing
    ):
        print(
            f"[check_orphan_keys] OK — {len(used_keys_map)} used keys and "
            f"{len(source_keys)} {SOURCE_LOCALE} keys verified across "
            f"{len(locale_files)} languages. No orphans, no missing translations."
        )
        if baseline:
            print(
                f"[check_orphan_keys] {len(baseline)} key(s) grandfathered as "
                f"unreferenced ({UNUSED_BASELINE.name}); this list may only shrink."
            )
        for key in revived:
            print(
                f"[check_orphan_keys] cleanup: '{key}' is referenced again — "
                f"remove it from {UNUSED_BASELINE.name}."
            )
        for key in stale_baseline:
            print(
                f"[check_orphan_keys] cleanup: '{key}' is no longer declared — "
                f"remove it from {UNUSED_BASELINE.name}."
            )
        return 0

    if locale_orphans:
        print(
            "[check_orphan_keys] FAIL — translation keys are used in code but NOT declared in target locales:",
            file=sys.stderr,
        )
        for locale_name, missing in sorted(locale_orphans.items()):
            print(f"\n[{locale_name}] Missing {len(missing)} keys:", file=sys.stderr)
            for key in missing:
                locations = used_keys_map[key]
                first_file, first_line = locations[0]
                rel_path = first_file.relative_to(REPO_ROOT)
                extra = f" (+{len(locations) - 1} more sites)" if len(locations) > 1 else ""
                print(f"  {key}  →  {rel_path}:{first_line}{extra}", file=sys.stderr)

    if locale_incomplete:
        print(
            f"\n[check_orphan_keys] FAIL — keys present in {SOURCE_LOCALE} are MISSING "
            f"from these locales (they will fall back to English):",
            file=sys.stderr,
        )
        for locale_name, missing in sorted(locale_incomplete.items()):
            print(f"\n[{locale_name}] Missing {len(missing)} keys:", file=sys.stderr)
            for key in missing:
                print(f"  {key}", file=sys.stderr)

    if backend_missing:
        print(
            f"\n[check_orphan_keys] FAIL — {len(backend_missing)} i18n key(s) "
            f"named by the BACKEND have no string in {SOURCE_LOCALE}:",
            file=sys.stderr,
        )
        for key, loc in backend_missing:
            print(f"  {key}  →  {loc}", file=sys.stderr)
        print(
            "\n  i18next renders a missing key as the key itself, so this is "
            "what\n  the user would read. Either add the string to all locale "
            "files, or\n  stop naming it — see ExitCode.user_message_key, which "
            "mapped eight\n  keys that were never written.",
            file=sys.stderr,
        )

    if newly_dead:
        print(
            f"\n[check_orphan_keys] FAIL — {len(newly_dead)} translation key(s) "
            f"are declared in all {len(locale_files)} locales but nothing "
            f"anywhere reaches them:",
            file=sys.stderr,
        )
        for key in newly_dead:
            print(f"  {key}", file=sys.stderr)
        print(
            "\n  A string written and translated 16 times with no delivery path "
            "is\n"
            "  the most repeated defect in the 2026-08 audit — the user sees "
            "nothing\n"
            "  while every gate stays green. Either wire it up (a backend "
            "i18n_key\n"
            "  counts), or delete the key from all locales. If it is genuinely\n"
            f"  reached in a way this scan cannot see, add it to "
            f"{UNUSED_BASELINE.name}\n"
            "  with a reason — but that list is meant to shrink, not grow.",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
