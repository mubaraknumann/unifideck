#!/usr/bin/env python3
"""scripts/check_untranslated.py — Catch locale values that were never translated.

The blind spot this closes
--------------------------
``check_orphan_keys.py`` proves every ``en-US`` key is *present* in the other
15 locales. It says nothing about whether the value was ever *translated*. A
key added to ``en-US`` and copied verbatim into the other files passes that
check cleanly while every non-English user reads English.

That is not hypothetical: the 0.7.4 Battle.net launcher toasts shipped that
way — 24 keys carrying the English source text in all 15 locales, with every
existing gate green.

What counts as a finding
------------------------
Two independent checks, either of which fails the run:

1. **Untranslated** — a ``(key, locale)`` pair whose value is byte-identical to
   ``en-US`` and is not covered by ``i18n_allowlist.json``.

2. **Placeholder drift** — a translation whose ``{{interpolation}}`` slots do
   not match the ``en-US`` source. A dropped slot silently deletes a value from
   the sentence, and an invented one renders as literal ``{{text}}``. This is
   not hypothetical either: ``toasts.cleanupSuccessfulMessage`` gained a
   ``{{residual}}`` count in ``en-US`` that 14 of 15 locales never picked up,
   so every non-English user lost that number from the sentence.

Two classes are skipped before the allowlist is consulted, because they can
never be findings:

* **Nothing to translate** — the value has no word of more than two letters
  once ``{{interpolation}}``, digits and punctuation are stripped (``"{{n}}%"``,
  ``":"``, ``"v{{version}}"``).
* **A bare brand name** — the whole value is a known product name
  (``"Battle.net"``, ``"Epic Games"``). Matched as a *complete phrase*, never
  token-wise: token matching would silently skip ``"Connect"`` the button label
  because ``"Ubisoft Connect"`` contains that word, and a silent skip is the
  exact failure this script exists to catch.

Everything else is an explicit decision recorded in the allowlist with a
reason, so "identical on purpose" and "nobody translated it" stay
distinguishable a year from now.

Usage
-----
    python3 scripts/check_untranslated.py

Exits non-zero if any unallowlisted pair is found. Also prints an advisory
(never failing) count of declared-but-unreferenced keys.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from check_orphan_keys import (
    LOCALES_DIR,
    SOURCE_LOCALE,
    flatten_json,
    scan_frontend_files,
)

ALLOWLIST_PATH = Path(__file__).resolve().parent / "i18n_allowlist.json"

# Values that are a complete product name are correctly identical in every
# locale. Compared against the whole value, lowercased, with punctuation
# collapsed to single spaces — see the module docstring on why this is not a
# token-wise test.
BRAND_PHRASES = frozenset({
    "amazon", "amazon games", "battle net", "blizzard", "comet", "decky",
    "decky loader", "discord", "epic", "epic games", "ge proton", "gog",
    "gogdl", "itch io", "legendary", "nile", "proton", "proton ge", "steam",
    "steam deck", "steamgriddb", "ubisoft", "ubisoft connect", "umu",
    "unifideck", "wine", "winetricks", "xbox", "xbox cloud gaming", "xcloud",
})

_INTERPOLATION = re.compile(r"\{\{[^}]*\}\}")
_NON_ALPHA = re.compile(r"[^a-z ]+")
_SLOT = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def _normalize(value: str) -> str:
    """Lowercase the value with interpolation and punctuation removed."""
    stripped = _INTERPOLATION.sub(" ", value.lower())
    return " ".join(_NON_ALPHA.sub(" ", stripped).split())


def is_translatable(value: str) -> bool:
    """True if the value carries prose a translator could actually change."""
    normalized = _normalize(value)
    if normalized in BRAND_PHRASES:
        return False
    return any(len(word) > 2 for word in normalized.split())


def is_doc_key(key: str) -> bool:
    """``_comment`` entries document the JSON itself and never reach the UI."""
    return key == "_comment" or key.endswith("._comment")


def is_singular_form(key: str, source: dict[str, str]) -> bool:
    """True if ``key`` is the count==1 member of an i18next plural group.

    i18next resolves ``foo`` for one and ``foo_other`` for the rest. A language
    may state "one entry" idiomatically without repeating the digit — Arabic
    writes ``سجل واحد``, not ``1 سجل`` — so the singular member is allowed to
    omit ``{{count}}``. The plural members are not: "{{count}} entries" without
    its number is a real defect.
    """
    if key.endswith(("_one", "_zero")):
        return True
    return f"{key}_other" in source


def load_allowlist() -> dict[str, set[str] | str]:
    """Parse the allowlist into ``{key: {"locale", ...} | "*"}``."""
    if not ALLOWLIST_PATH.exists():
        return {}
    try:
        with ALLOWLIST_PATH.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        print(
            f"[check_untranslated] error: failed to parse {ALLOWLIST_PATH}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    parsed: dict[str, set[str] | str] = {}
    for key, entry in raw.items():
        if key.startswith("_"):
            continue  # schema documentation inside the allowlist file
        locales = entry.get("locales") if isinstance(entry, dict) else None
        if locales == "*":
            parsed[key] = "*"
        elif isinstance(locales, list):
            parsed[key] = set(locales)
        else:
            print(
                f"[check_untranslated] error: allowlist entry {key!r} needs "
                '"locales" as a list of tags or "*"',
                file=sys.stderr,
            )
            raise SystemExit(2)
    return parsed


def is_allowed(
    allowlist: dict[str, set[str] | str], key: str, locale: str,
) -> bool:
    entry = allowlist.get(key)
    if entry is None:
        return False
    return entry == "*" or locale in entry


def report_dead_keys(source: dict[str, str]) -> None:
    """Advisory only: keys declared in en-US that no literal ``t()`` names.

    Never fails the run. Many keys legitimately reach ``t()`` through a helper
    (``t(statusLabelKey(...))``) and are invisible to a literal scan. It is
    still worth printing: a key can rot into disuse while English sits
    hardcoded beside it.
    """
    used = set(scan_frontend_files())
    declared = {k for k in source if not is_doc_key(k)}
    unreferenced = declared - used
    print(
        f"[check_untranslated] advisory — {len(unreferenced)} of "
        f"{len(declared)} {SOURCE_LOCALE} keys are not named by a literal "
        "t() call (many resolve through helpers; not a failure).",
    )


def load_locales() -> dict[str, dict[str, str]]:
    """Flatten every locale file. Raises SystemExit(2) if any cannot be read."""
    locale_files = sorted(LOCALES_DIR.glob("*.json"))
    if not locale_files:
        print(
            f"[check_untranslated] error: no locale files in {LOCALES_DIR}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    flat_by_locale: dict[str, dict[str, str]] = {}
    for path in locale_files:
        try:
            with path.open(encoding="utf-8") as handle:
                flat_by_locale[path.stem] = flatten_json(json.load(handle))
        except Exception as exc:
            print(
                f"[check_untranslated] error: failed to parse {path}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc

    if SOURCE_LOCALE not in flat_by_locale:
        print(
            f"[check_untranslated] error: {SOURCE_LOCALE}.json not found",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return flat_by_locale


def find_untranslated(
    candidates: dict[str, str],
    flat: dict[str, str],
    allowlist: dict[str, set[str] | str],
    locale: str,
) -> tuple[list[str], int]:
    """Keys whose value still equals en-US, split from the allowlisted ones."""
    untranslated: list[str] = []
    allowed = 0
    for key, value in sorted(candidates.items()):
        if flat.get(key) != value:
            continue
        if is_allowed(allowlist, key, locale):
            allowed += 1
        else:
            untranslated.append(key)
    return untranslated, allowed


def find_drift(
    slotted: dict[str, set[str]], flat: dict[str, str], source: dict[str, str],
) -> list[str]:
    """Keys whose {{placeholders}} do not match the en-US source."""
    mismatched: list[str] = []
    for key, expected in sorted(slotted.items()):
        got = set(_SLOT.findall(flat.get(key, "")))
        want = expected
        if is_singular_form(key, source):
            want = expected - {"count"}
            got = got - {"count"}
        if got != want:
            lost = ", ".join(sorted(want - got)) or "-"
            extra = ", ".join(sorted(got - want)) or "-"
            mismatched.append(f"{key}  (missing: {lost}; unexpected: {extra})")
    return mismatched


def report_failures(
    findings: dict[str, list[str]],
    drift: dict[str, list[str]],
    source: dict[str, str],
) -> None:
    """Print both failure reports to stderr."""
    if findings:
        total = sum(len(v) for v in findings.values())
        print(
            f"[check_untranslated] FAIL — {total} value(s) across "
            f"{len(findings)} locale(s) are still the {SOURCE_LOCALE} source "
            "text. Translate them, or add a reasoned entry to "
            f"{ALLOWLIST_PATH.name}:",
            file=sys.stderr,
        )
        for locale, keys in findings.items():
            print(f"\n[{locale}] {len(keys)} untranslated:", file=sys.stderr)
            for key in keys:
                print(f"  {key}  =  {source[key][:70]!r}", file=sys.stderr)

    if drift:
        total = sum(len(v) for v in drift.values())
        print(
            f"\n[check_untranslated] FAIL — {total} translation(s) do not "
            f"carry the same {{{{placeholders}}}} as {SOURCE_LOCALE}. A missing "
            "slot drops its value from the sentence; an unexpected one renders "
            "literally:",
            file=sys.stderr,
        )
        for locale, entries in drift.items():
            print(f"\n[{locale}] {len(entries)} mismatched:", file=sys.stderr)
            for entry in entries:
                print(f"  {entry}", file=sys.stderr)


def main() -> int:
    flat_by_locale = load_locales()
    source = flat_by_locale[SOURCE_LOCALE]
    candidates = {
        key: value
        for key, value in source.items()
        if not is_doc_key(key) and is_translatable(value)
    }
    # Placeholder parity is checked against every key with a slot, not just the
    # translatable ones: a value can be a bare "{{count}}" and still lose it.
    slotted = {
        key: set(_SLOT.findall(value))
        for key, value in source.items()
        if not is_doc_key(key) and _SLOT.search(value)
    }

    allowlist = load_allowlist()
    findings: dict[str, list[str]] = {}
    drift: dict[str, list[str]] = {}
    allowed_hits = 0
    for locale, flat in sorted(flat_by_locale.items()):
        if locale == SOURCE_LOCALE:
            continue
        untranslated, allowed = find_untranslated(
            candidates, flat, allowlist, locale,
        )
        allowed_hits += allowed
        if untranslated:
            findings[locale] = untranslated
        mismatched = find_drift(slotted, flat, source)
        if mismatched:
            drift[locale] = mismatched

    if not findings and not drift:
        print(
            f"[check_untranslated] OK — {len(candidates)} translatable "
            f"{SOURCE_LOCALE} keys checked across {len(flat_by_locale) - 1} "
            f"locales. No untranslated values ({allowed_hits} allowlisted), "
            f"{len(slotted)} interpolated keys match placeholders.",
        )
        report_dead_keys(source)
        return 0

    report_failures(findings, drift, source)
    return 1


if __name__ == "__main__":
    sys.exit(main())
