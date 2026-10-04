"""Title normalisation and version-number extraction.

Split out of the former monolithic ``title_match.py`` (2026-09-26, file-size
gate) purely by concern — the fixed-point suffix strippers, the sequel/
version guard, and the string-cleanup pass have no shared state and were
already the natural top of the module.
"""
from __future__ import annotations

import re
import unicodedata

# Multi-character Roman numerals → Arabic, for version folding ("Thief II"
# ↔ "Thief 2"). Single letters (I/V/X) are deliberately excluded — they're
# too often branding ("Mega Man X", "Entropy Effect X") rather than a
# version, and folding them risks false matches.
_ROMAN_TO_ARABIC: dict[str, str] = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8",
    "ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
}


def version_tokens(normalized: str) -> frozenset[str]:
    """Sequel discriminators in a normalised title, folded to Arabic.

    The *one* part of a title that must survive every suffix strip. Two
    titles that differ only here are different games, so the strippers
    in :mod:`.edition_suffixes` refuse to consume these and
    :func:`.matching.titles_match` refuses to match across a mismatch.

    Counts multi-character Roman numerals (via ``_ROMAN_TO_ARABIC``, so
    "II" and "2" agree) and bare integers. Deliberately NOT counted:

    * 4-digit years 1980-2030 — an edition tag, not a sequel number, so
      "Sea of Thieves" still matches "Sea of Thieves: 2026 Edition".
      Anno's 1404 / 1701 / 1602 fall outside that window and DO count,
      which is exactly right: they name different games.
    * ordinals ("10th") and any token with non-digit characters — those
      are words, not version markers.

    Pure function. Empty input, or a title with no version marker,
    returns an empty set (which is itself a value: "no number" differs
    from "number 7", so ``The Settlers`` won't match ``The Settlers 7``).
    """
    tokens: set[str] = set()
    for word in normalized.split():
        roman = _ROMAN_TO_ARABIC.get(word)
        if roman is not None:
            tokens.add(roman)
        elif word.isdigit() and not (
            len(word) == 4 and 1980 <= int(word) <= 2030
        ):
            tokens.add(str(int(word)))
    return frozenset(tokens)


def normalize_for_match(title: str) -> str:
    """Lowercase + strip symbols + collapse whitespace.

    Steps in order:

    1. lowercase + trim;
    2. dual-language "Game / Jeu" → first half;
    3. ® ™ © → space (preserves word boundaries: ``Watch Dogs®2``
       becomes ``watch dogs 2`` not ``watch dogs2``);
    4. NFKD-decompose + strip combining marks (é→e, ü→u);
    5. strip ``(TM)`` ``(R)`` ``(C)``;
    6. ``&`` → ``and``;
    7. smart-quotes → ASCII;
    8. ``_`` / ``-`` → space;
    9. ``|`` → empty (so ``X|S`` becomes ``XS`` not ``X S``);
    10. remaining punctuation → space;
    11. collapse runs of whitespace.

    Returns the normalised string. Empty input returns empty string.
    """
    if not title:
        return ""
    t = title.lower().strip()
    if " / " in t:
        t = t.split(" / ", 1)[0].strip()
    t = re.sub(r"[®™©]", " ", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\((?:tm|r|c)\)", "", t, flags=re.IGNORECASE)
    t = t.replace("&", " and ")
    t = t.replace("‘", "'").replace("’", "'")  # noqa: RUF001  smart-quote match is intentional
    t = t.replace("“", '"').replace("”", '"')
    t = t.replace("_", " ").replace("-", " ").replace("|", "")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fold_roman_numerals(normalized: str) -> str:
    """Fold multi-char Roman-numeral tokens to Arabic in a normalised title."""
    return " ".join(_ROMAN_TO_ARABIC.get(w, w) for w in normalized.split())
